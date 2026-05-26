#!/usr/bin/env python3
"""
Creative Studio Web App v4.5
Flask backend with session management, cost tracking, generation, composite, export, QC.
Serves built-in frontend template.
"""

import os
import sys
import json
import time
import uuid
import re
import subprocess
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict
from functools import wraps

from flask import Flask, render_template_string, request, jsonify, send_from_directory, send_file

from figma_utils import parse_figma_url, fetch_figma_context, enhance_prompt_with_figma

# ─── Config ────────────────────────────────────────────────────────────
# Server fallback key (optional). When absent, app runs in BYOK mode.
SERVER_API_KEY = os.environ.get("GEMINI_API_KEY", "")


def _get_api_key() -> str:
    """Return the active API key: user's X-API-Key header, or server fallback, or empty."""
    user_key = request.headers.get("X-API-Key", "").strip()
    if user_key:
        return user_key
    return SERVER_API_KEY


# Session / cost / output dirs
if os.environ.get("CREATIVE_DATA_DIR"):
    DATA_DIR = Path(os.environ["CREATIVE_DATA_DIR"])
else:
    DATA_DIR = Path.home() / ".creative-studio-data"
SESSIONS_DIR = DATA_DIR / "sessions"
COST_DB = DATA_DIR / "costs.json"

# Match CLI output directory logic exactly
if os.environ.get("CREATIVE_OUTPUT_DIR"):
    OUTPUT_DIR = Path(os.environ["CREATIVE_OUTPUT_DIR"])
elif Path("/mnt/c/Users").exists():
    _win_dl = Path(os.environ.get("CREATIVE_OUTPUT_DIR", str(Path.home() / "Downloads" / "creative-studio-outputs")))
    _win_dl.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR = _win_dl
else:
    OUTPUT_DIR = Path.home() / "creative-studio-outputs"

DATA_DIR.mkdir(parents=True, exist_ok=True)
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(Path(__file__).parent))

# ─── Helpers ───────────────────────────────────────────────────────────


def load_json(path: Path, default=None):
    return (
        json.loads(path.read_text())
        if path.exists()
        else (default if default is not None else {})
    )


def save_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


# ─── Cost tracking ───────────────────────────────────────────────────
COSTS = {
    "gemini-3.1-flash-image-preview": 0.07,
    "gemini-3-pro-image-preview": 0.20,
    "imagen-4.0-fast-generate-001": 0.02,
    "imagen-4.0-generate-001": 0.04,
    "imagen-4.0-ultra-generate-001": 0.06,
}


def load_costs():
    return load_json(
        COST_DB,
        {
            "total": 0.0,
            "by_model": {},
            "by_date": {},
            "session_count": 0,
            "image_count": 0,
        },
    )


def save_costs(data: dict):
    save_json(COST_DB, data)


def track_cost(model: str, count: int = 1):
    costs = load_costs()
    c = COSTS.get(model, 0.04) * count
    costs["total"] += c
    costs["by_model"][model] = costs["by_model"].get(model, 0.0) + c
    today = datetime.now().strftime("%Y-%m-%d")
    costs["by_date"][today] = costs["by_date"].get(today, 0.0) + c
    costs["image_count"] += count
    save_costs(costs)
    return c


def session_cost(session_id: str) -> float:
    return sum(e.get("cost", 0) for e in load_session(session_id).get("entries", []))


# ─── Session management ────────────────────────────────────────────────


def new_session_id():
    return "sess_" + uuid.uuid4().hex[:8]


def session_path(session_id: str) -> Path:
    return SESSIONS_DIR / f"{session_id}.json"


def load_session(session_id: str) -> dict:
    return load_json(
        session_path(session_id),
        {"id": session_id, "created_at": now_str(), "entries": []},
    )


def save_session(session_id: str, data: dict):
    save_json(session_path(session_id), data)


def add_entry(session_id: str, entry: dict):
    data = load_session(session_id)
    data["entries"].append({"time": now_str(), **entry})
    save_session(session_id, data)


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def image_url(path: str) -> str:
    if not path:
        return ""
    p = Path(path)
    try:
        rel = p.relative_to(OUTPUT_DIR)
        return f"/image/{rel}"
    except (ValueError, NotImplementedError):
        if p.exists():
            return f"/image/{p.parent.name}/{p.name}"
        return ""


# ─── Pin Annotations ────────────────────────────────────────────────────
PINS_DB = DATA_DIR / "pins.json"


def load_pins(image_path: str) -> List[Dict]:
    data = load_json(PINS_DB, {})
    return data.get(image_path, [])


def save_pins(image_path: str, pins: List[Dict]):
    data = load_json(PINS_DB, {})
    data[image_path] = pins
    save_json(PINS_DB, data)


def pin_id() -> str:
    return uuid.uuid4().hex[:8]


def pin_to_region(x: float, y: float) -> str:
    """Convert normalized coords (0..1) to human-readable region."""
    h = "top" if y < 0.33 else "middle" if y < 0.66 else "bottom"
    w = "left" if x < 0.33 else "center" if x < 0.66 else "right"
    if h == "middle" and w == "center":
        return "center"
    return f"{h}-{w}" if h != "middle" else w


def build_pin_prompt(pins: List[Dict]) -> str:
    """Build a spatially-aware refinement prompt from pins."""
    if not pins:
        return ""
    lines = []
    for i, p in enumerate(pins, 1):
        region = pin_to_region(p.get("x", 0.5), p.get("y", 0.5))
        text = p.get("text", "").strip()
        if text:
            lines.append(f"[{i}] {region}: {text}")
    if not lines:
        return ""
    return (
        "Make these targeted changes to specific areas of the image: "
        + "; ".join(lines)
        + ". Apply each change only to its specified region. Preserve all other areas exactly as they are."
    )


# ─── Image generation wrappers ────────────────────────────────────────

SCRIPT_PATH = str(Path(__file__).parent / "creative_studio.py")

# Tier → (model, resolution) — must match creative_studio.py _TIER_MAP
_TIER_MODEL = {
    "fast": "gemini-3.1-flash-image-preview",
    "balanced": "gemini-3.1-flash-image-preview",
    "quality": "gemini-3-pro-image-preview",
    "ultra": "gemini-3-pro-image-preview",
}
_TIER_COST = {
    "fast": 0.07,
    "balanced": 0.07,
    "quality": 0.20,
    "ultra": 0.20,
}


def run_cli_generate(
    prompt: str,
    mode: str,
    api_key: str,
    tier: str,
    aspect: str,
    smart: bool,
    input_image: Optional[str] = None,
    variations: int = 4,
) -> List[Dict]:
    """Generate images by calling creative_studio.py directly (no bash/uv wrapper).
    If variations > 1, run the direct command multiple times and collect outputs."""
    today = datetime.now().strftime("%Y-%m-%d")
    out_dir = OUTPUT_DIR / today / mode
    out_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["GEMINI_API_KEY"] = api_key
    env["CREATIVE_OUTPUT_DIR"] = str(OUTPUT_DIR)

    images = []
    count = max(1, min(8, variations))
    for i in range(count):
        args = [
            sys.executable,
            SCRIPT_PATH,
            "direct",
            "--prompt",
            prompt,
            "--tier",
            tier,
            "--aspect-ratio",
            aspect,
        ]
        if smart:
            args.append("--smart")
        if input_image:
            args += ["--input-image", input_image]

        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, timeout=300, env=env, check=True
            )
        except subprocess.CalledProcessError as e:
            if i == 0:
                return [{"error": f"Generation failed: {e.stderr[:500] if e.stderr else e}"}]
            break  # Return what we have so far
        except Exception as e:
            if i == 0:
                return [{"error": str(e)}]
            break

        # Collect the single most-recent file from this run
        today_dir = OUTPUT_DIR / today
        files = sorted(
            today_dir.rglob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        now = time.time()
        recent = [f for f in files if (now - f.stat().st_mtime) < 180]
        if recent:
            f = recent[0]
            model_used = (
                "gemini-3.1-flash-image-preview"
                if tier in ("fast", "balanced")
                else "gemini-3-pro-image-preview"
            )
            cost = track_cost(model_used)
            images.append(
                {
                    "path": str(f),
                    "url": image_url(str(f)),
                    "name": f.name,
                    "cost": cost,
                    "model": model_used,
                    "ratio": aspect,
                }
            )

    if not images:
        return [{"error": "Generation produced no output"}]
    return images


def run_cli_composite(
    prompt: str, product_path: str, api_key: str, aspect: str, tier: str = "quality"
) -> List[Dict]:
    out_dir = OUTPUT_DIR / datetime.now().strftime("%Y-%m-%d") / "composite"
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"composite-{int(time.time())}.png"
    out_path = out_dir / fname

    args = [
        sys.executable,
        SCRIPT_PATH,
        "composite",
        "--prompt",
        prompt,
        "--product",
        product_path,
        "--aspect-ratio",
        aspect,
        "--tier",
        tier,
        "--filename",
        fname,
    ]
    env = os.environ.copy()
    env["GEMINI_API_KEY"] = api_key

    try:
        subprocess.run(
            args, capture_output=True, text=True, timeout=300, env=env, check=True
        )
        if out_path.exists():
            model_used = "gemini-3-pro-image-preview"
            cost = track_cost(model_used)
            return [
                {
                    "path": str(out_path),
                    "url": image_url(str(out_path)),
                    "name": fname,
                    "cost": cost,
                    "model": model_used,
                    "ratio": aspect,
                }
            ]
    except subprocess.CalledProcessError as e:
        return [{"error": f"Composite failed: {e.stderr[:500] if e.stderr else e}"}]
    except Exception as e:
        return [{"error": str(e)}]
    return [{"error": "Composite produced no output"}]


def run_cli_export(source_path: str, presets: str, api_key: str) -> List[Dict]:
    args = [
        "bash",
        str(Path(__file__).parent.parent / "launch.sh"),
        "export",
        "--input",
        source_path,
        "--presets",
        presets,
    ]
    env = os.environ.copy()
    env["GEMINI_API_KEY"] = api_key
    env["CREATIVE_OUTPUT_DIR"] = str(OUTPUT_DIR)
    try:
        subprocess.run(
            args, capture_output=True, text=True, timeout=120, env=env, check=True
        )
        out_dir = OUTPUT_DIR / datetime.now().strftime("%Y-%m-%d") / "exports"
        files = list(out_dir.glob("*.png"))
        images = []
        for f in files:
            images.append(
                {
                    "path": str(f),
                    "url": image_url(str(f)),
                    "name": f.name,
                    "cost": 0.0,
                    "model": "PIL",
                }
            )
        return images
    except subprocess.CalledProcessError as e:
        return [{"error": f"Export failed: {e.stderr[:500] if e.stderr else e}"}]
    except Exception as e:
        return [{"error": str(e)}]


def run_cli_qc(image_path: str, api_key: str) -> dict:
    args = [
        "bash",
        str(Path(__file__).parent.parent / "launch.sh"),
        "qc",
        "--input",
        image_path,
    ]
    env = os.environ.copy()
    env["GEMINI_API_KEY"] = api_key
    # Parse stdout for QC results
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=120, env=env, check=True
        )
        out = result.stdout + result.stderr
        # Extract score
        score = 5
        m = re.search(r"QC SCORE:\s*(\d)/10", out)
        if m:
            score = int(m.group(1))
        floating = "FAIL" in out and "Floating" in out
        garbled = "FAIL" in out and "Garbled" in out
        shadows = "FAIL" in out and "Shadows" in out
        fake = "FAIL" in out and "Fake" in out
        labels = "PASS" in out and "Labels" in out
        # Extract issues
        issues = []
        for line in out.split("\n"):
            if "⚠" in line:
                issues.append(line.replace("⚠", "").strip())
        return {
            "quality_score": score,
            "floating_products": floating,
            "garbled_text": garbled,
            "detached_shadows": shadows,
            "fake_products": fake,
            "readable_labels": labels,
            "issues": issues[:5],
        }
    except subprocess.CalledProcessError as e:
        return {"quality_score": 0, "error": f"QC failed: {e.stderr[:500] if e.stderr else e}", "issues": []}
    except Exception as e:
        return {"quality_score": 0, "error": str(e), "issues": []}


def run_cli_refine(image_path: str, changes: str, api_key: str, tier: str) -> List[Dict]:
    # Since refine needs a session folder from variations, we'll do a "revise" via direct mode
    # with the original image as reference + changes in prompt
    out_dir = OUTPUT_DIR / datetime.now().strftime("%Y-%m-%d") / "refine"
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"refine-{int(time.time())}.png"

    # Build revised prompt
    args = [
        "bash",
        str(Path(__file__).parent.parent / "launch.sh"),
        "direct",
        "--prompt",
        f"Based on this reference image, make these changes: {changes}",
        "--input-image",
        image_path,
        "--tier",
        tier,
        "--filename",
        fname,
    ]
    env = os.environ.copy()
    env["GEMINI_API_KEY"] = api_key
    try:
        subprocess.run(
            args, capture_output=True, text=True, timeout=300, env=env, check=True
        )
        out_path = out_dir / fname
        if not out_path.exists():
            # Fallback: search for any newly created png in output dir
            today_dir = OUTPUT_DIR / datetime.now().strftime("%Y-%m-%d")
            files = sorted(
                today_dir.rglob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True
            )
            if files:
                out_path = files[0]
        if out_path.exists():
            model_used = (
                "gemini-3.1-flash-image-preview"
                if tier in ("fast", "balanced")
                else "gemini-3-pro-image-preview"
            )
            cost = track_cost(model_used)
            return [
                {
                    "path": str(out_path),
                    "url": image_url(str(out_path)),
                    "name": out_path.name,
                    "cost": cost,
                    "model": model_used,
                }
            ]
    except subprocess.CalledProcessError as e:
        return [{"error": f"Refine failed: {e.stderr[:500] if e.stderr else e}"}]
    except Exception as e:
        return [{"error": str(e)}]
    return []


# Variations angle/lighting suffix templates — match CLI cmd_variations exactly
_VARIATION_SUFFIXES = [
    " eye-level composition. warm 3200K overhead lighting. shallow depth of field with creamy bokeh. Professional product photography.",
    " slightly low angle hero shot. neutral 5600K soft-diffused lighting. deep depth of field. Professional product photography.",
    " three-quarter view composition. crisp directional rim light. selective focus on hero product. Professional product photography.",
    " straight-on composition. even flat ambient lighting. sharp throughout with slight falloff. Professional product photography.",
    " eye-level composition. neutral 5600K soft-diffused lighting. shallow depth of field with creamy bokeh. Professional product photography.",
    " slightly low angle hero shot. warm 3200K overhead lighting. deep depth of field. Professional product photography.",
    " three-quarter view composition. even flat ambient lighting. selective focus on hero product. Professional product photography.",
    " straight-on composition. crisp directional rim light. sharp throughout with slight falloff. Professional product photography.",
]


def run_cli_variations(
    prompt: str,
    api_key: str,
    count: int,
    tier: str,
    aspect: str,
    input_image: Optional[str] = None,
) -> tuple[List[Dict], str]:
    """
    Generate N variations using the same brief + different angle/lighting suffixes.
    Returns (images, session_key) where session_key is used by refine.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    out_dir = OUTPUT_DIR / today / "variations"
    out_dir.mkdir(parents=True, exist_ok=True)
    session_key = f"vars-{int(time.time())}"
    session_dir = out_dir / session_key
    session_dir.mkdir(parents=True, exist_ok=True)

    images = []
    for i in range(count):
        vname = f"v{i+1:02d}.png"
        vpath = session_dir / vname
        variation_prompt = (
            prompt
            + "\n\n"
            + _VARIATION_SUFFIXES[i % len(_VARIATION_SUFFIXES)]
            + " The shelf surface is perfectly flat and level. Products sit firmly with flat bases touching the shelf. No tilting, no floating, no falling."
        )

        args = [
            sys.executable,
            SCRIPT_PATH,
            "direct",
            "--prompt",
            variation_prompt,
            "--tier",
            tier,
            "--aspect-ratio",
            aspect,
            "--filename",
            str(vpath),
        ]
        if input_image:
            args += ["--input-image", input_image]

        env = os.environ.copy()
        env["GEMINI_API_KEY"] = api_key
        env["CREATIVE_OUTPUT_DIR"] = str(OUTPUT_DIR)

        try:
            subprocess.run(
                args, capture_output=True, text=True, timeout=300, env=env, check=True
            )
            if vpath.exists():
                model_used = _TIER_MODEL.get(tier, "gemini-3-pro-image-preview")
                cost = track_cost(model_used)
                images.append(
                    {
                        "path": str(vpath),
                        "url": image_url(str(vpath)),
                        "name": vname,
                        "cost": cost,
                        "model": model_used,
                        "variation_index": i + 1,
                    }
                )
        except subprocess.CalledProcessError:
            # Continue with remaining variations
            pass

    # Save manifest so refine can find the session
    manifest = {
        "count": len(images),
        "model": _TIER_MODEL.get(tier, "gemini-3-pro-image-preview"),
        "resolution": "2K",
        "original_prompt": prompt,
        "tier": tier,
        "aspect_ratio": aspect,
        "files": [img["path"] for img in images],
        "prompts": [
            prompt + _VARIATION_SUFFIXES[i % len(_VARIATION_SUFFIXES)]
            for i in range(len(images))
        ],
    }
    (session_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return images, session_key


def run_cli_refine_from_variation(
    session_key: str,
    pick_index: int,
    changes: str,
    tier: str,
) -> List[Dict]:
    """
    Refine a specific variation by its 1-based index.
    Uses the manifest written by run_cli_variations.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    session_dir = OUTPUT_DIR / today / "variations" / session_key
    manifest_path = session_dir / "manifest.json"

    if not manifest_path.exists():
        # Try searching older dates
        for date_dir in sorted(OUTPUT_DIR.iterdir(), reverse=True):
            if date_dir.is_dir():
                candidate = date_dir / "variations" / session_key
                if candidate.exists():
                    session_dir = candidate
                    manifest_path = session_dir / "manifest.json"
                    break

    if not manifest_path.exists():
        return [{"error": f"Session not found: {session_key}"}]

    manifest = json.loads(manifest_path.read_text())
    files = manifest.get("files", [])
    idx = pick_index - 1
    if idx < 0 or idx >= len(files):
        return [{"error": f"Pick must be between 1 and {len(files)}"}]

    base_path = files[idx]
    ref_prompt = manifest.get("prompts", [manifest["original_prompt"]])[idx]
    final_prompt = (
        f"Refinement based on version v{pick_index}:\n{changes}\n\n"
        f"Original prompt:\n{ref_prompt}"
    )

    out_dir = session_dir
    fname = f"r{pick_index:02d}-{int(time.time())}.png"
    out_path = out_dir / fname

    args = [
        sys.executable,
        SCRIPT_PATH,
        "direct",
        "--prompt",
        final_prompt,
        "--input-image",
        base_path,
        "--tier",
        tier,
        "--aspect-ratio",
        manifest.get("aspect_ratio", "16:9"),
        "--filename",
        str(out_path),
    ]

    env = os.environ.copy()
    env["GEMINI_API_KEY"] = api_key
    env["CREATIVE_OUTPUT_DIR"] = str(OUTPUT_DIR)

    try:
        subprocess.run(
            args, capture_output=True, text=True, timeout=300, env=env, check=True
        )
        if out_path.exists():
            model_used = _TIER_MODEL.get(tier, "gemini-3-pro-image-preview")
            cost = track_cost(model_used)
            return [
                {
                    "path": str(out_path),
                    "url": image_url(str(out_path)),
                    "name": out_path.name,
                    "cost": cost,
                    "model": model_used,
                }
            ]
    except subprocess.CalledProcessError as e:
        return [{"error": f"Refine failed: {e.stderr[:500] if e.stderr else e}"}]
    except Exception as e:
        return [{"error": str(e)}]
    return [{"error": "Refine produced no output"}]


# ─── Chat multi-turn state ──────────────────────────────────────────────
_chat_sessions: Dict[str, dict] = {}  # session_key → {turn, current_input, history}


def run_cli_chat_turn(
    session_key: str,
    api_key: str,
    prompt: str,
    tier: str,
    aspect: str,
    input_image: Optional[str] = None,
) -> tuple[List[Dict], dict]:
    """
    Single turn of the multi-turn chat workflow.
    Each result feeds into the next turn as the input image.
    Returns (images, session_state).
    """
    today = datetime.now().strftime("%Y-%m-%d")
    out_dir = OUTPUT_DIR / today / "chat"
    out_dir.mkdir(parents=True, exist_ok=True)

    if session_key not in _chat_sessions:
        _chat_sessions[session_key] = {
            "turn": 0,
            "current_input": input_image,
            "initial_input": input_image,
            "history": [],
        }

    sess = _chat_sessions[session_key]
    sess["turn"] += 1
    turn = sess["turn"]

    fname = f"turn-{turn:02d}.png"
    out_path = out_dir / f"{session_key}" / fname
    out_path.parent.mkdir(parents=True, exist_ok=True)

    args = [
        sys.executable,
        SCRIPT_PATH,
        "direct",
        "--prompt",
        prompt,
        "--tier",
        tier,
        "--aspect-ratio",
        aspect,
        "--filename",
        str(out_path),
    ]
    current_input = sess["current_input"]
    if current_input:
        args += ["--input-image", current_input]

    env = os.environ.copy()
    env["GEMINI_API_KEY"] = api_key
    env["CREATIVE_OUTPUT_DIR"] = str(OUTPUT_DIR)

    images = []
    try:
        subprocess.run(
            args, capture_output=True, text=True, timeout=300, env=env, check=True
        )
        if out_path.exists():
            model_used = _TIER_MODEL.get(tier, "gemini-3-pro-image-preview")
            cost = track_cost(model_used)
            images.append(
                {
                    "path": str(out_path),
                    "url": image_url(str(out_path)),
                    "name": fname,
                    "cost": cost,
                    "model": model_used,
                    "turn": turn,
                }
            )
            # Feed this output as input for next turn
            sess["current_input"] = str(out_path)
            sess["history"].append(
                {
                    "turn": turn,
                    "prompt": prompt,
                    "input": current_input,
                    "output": str(out_path),
                }
            )
    except subprocess.CalledProcessError as e:
        sess["turn"] -= 1  # rollback on failure
        return [{"error": f"Generation failed: {e.stderr[:500] if e.stderr else e}"}], sess
    except Exception as e:
        sess["turn"] -= 1
        return [{"error": str(e)}], sess

    return images, sess


def chat_session_history(session_key: str) -> List[dict]:
    sess = _chat_sessions.get(session_key, {})
    return sess.get("history", [])


def chat_reset(session_key: str) -> dict:
    sess = _chat_sessions.get(session_key, {})
    sess["turn"] = 0
    sess["current_input"] = sess.get("initial_input")
    sess["history"] = []
    _chat_sessions[session_key] = sess
    return sess


# ─── Flask App ─────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(32))
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32MB uploads

# ── Simple in-memory rate limiter ───────────────────────────────────────
_request_log: Dict[str, list] = {}
_RATE_LIMIT = 20  # requests per minute per IP

def rate_limited(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        ip = request.headers.get("X-Forwarded-For", request.remote_addr) or "unknown"
        now = time.time()
        _request_log.setdefault(ip, [])
        # purge old
        _request_log[ip] = [t for t in _request_log[ip] if now - t < 60]
        if len(_request_log[ip]) >= _RATE_LIMIT:
            return jsonify({"error": "Rate limit exceeded. Try again later."}), 429
        _request_log[ip].append(now)
        return f(*args, **kwargs)
    return wrapper

# ── Async Job System ──────────────────────────────────────────────────
_jobs: Dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _job_id() -> str:
    return "job_" + uuid.uuid4().hex[:12]


def _run_job_background(
    job_id: str,
    fn,
    *args,
    **kwargs,
):
    """Run a long-running generation function in a background thread."""

    def _worker():
        try:
            result = fn(*args, **kwargs)
            with _jobs_lock:
                _jobs[job_id]["status"] = "done"
                _jobs[job_id]["result"] = result
                _jobs[job_id]["finished_at"] = time.time()
        except Exception as e:
            with _jobs_lock:
                _jobs[job_id]["status"] = "error"
                _jobs[job_id]["error"] = str(e)
                _jobs[job_id]["finished_at"] = time.time()

    with _jobs_lock:
        _jobs[job_id] = {
            "status": "running",
            "started_at": time.time(),
            "result": None,
            "error": None,
            "finished_at": None,
        }
    t = threading.Thread(target=_worker, daemon=True)
    t.start()


# ── Frontend HTML ─────────────────────────────────────────────────────

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Creative Studio — AI Product Photography</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

:root {
  --bg: #0c0c10;
  --bg-elevated: #141419;
  --bg-hover: #1a1a22;
  --surface: #1e1e28;
  --surface-hover: #262632;
  --border: rgba(255,255,255,0.06);
  --border-strong: rgba(255,255,255,0.10);
  --text: #f2f2f7;
  --text-secondary: #a1a1aa;
  --text-dim: #71717a;
  --accent: #ff6b35;
  --accent-hover: #ff7f4d;
  --accent-glow: rgba(255,107,53,0.12);
  --accent-glow-strong: rgba(255,107,53,0.20);
  --success: #22c55e;
  --danger: #ef4444;
  --radius: 14px;
  --radius-sm: 8px;
  --radius-xs: 6px;
  --shadow: 0 1px 3px rgba(0,0,0,0.3), 0 0 0 1px var(--border);
  --font: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
}

* { box-sizing: border-box; margin: 0; padding: 0; }
html { scroll-behavior: smooth; }
body {
  font-family: var(--font);
  background: var(--bg);
  color: var(--text);
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
  min-height: 100vh;
}

/* ── Header ── */
.header {
  position: sticky; top: 0; z-index: 50;
  background: rgba(12,12,16,0.85);
  backdrop-filter: blur(16px);
  border-bottom: 1px solid var(--border);
}
.header-inner {
  max-width: 1280px; margin: 0 auto;
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 24px; height: 60px;
}
.header-brand {
  display: flex; align-items: center; gap: 10px; text-decoration: none;
}
.header-brand .mark {
  width: 28px; height: 28px; background: var(--accent);
  border-radius: 7px; display: flex; align-items: center; justify-content: center;
  font-size: 14px; color: #fff; font-weight: 700;
}
.header-brand .name {
  font-size: 0.95rem; font-weight: 700; color: var(--text); letter-spacing: -0.01em;
}
.header-nav { display: flex; align-items: center; gap: 4px; }
.header-nav a {
  padding: 6px 14px; border-radius: var(--radius-xs);
  font-size: 0.82rem; font-weight: 500; color: var(--text-secondary);
  text-decoration: none; transition: all 0.2s;
}
.header-nav a:hover { color: var(--text); background: var(--bg-hover); }
.header-nav a.active { color: var(--text); background: var(--bg-hover); }

.credits-pill {
  display: flex; align-items: center; gap: 8px;
  padding: 5px 14px; border-radius: 100px;
  border: 1px solid var(--border); background: var(--bg-elevated);
  font-size: 0.78rem; color: var(--text-secondary);
}
.credits-pill .amt { font-weight: 600; color: var(--text); }
.credits-pill .sep { color: var(--border-strong); }
.upgrade-btn {
  font-size: 0.75rem; font-weight: 600; color: var(--accent);
  text-decoration: none; padding: 3px 10px; border-radius: 100px;
  border: 1px solid rgba(255,107,53,0.25); background: var(--accent-glow);
  transition: all 0.15s; cursor: pointer;
}
.upgrade-btn:hover { background: var(--accent-glow-strong); }

/* ── Layout ── */
.main {
  max-width: 1280px; margin: 0 auto;
  padding: 32px 24px 80px;
  display: grid;
  grid-template-columns: 380px 1fr;
  gap: 32px;
}
@media (max-width: 960px) {
  .main { grid-template-columns: 1fr; }
}

.sidebar { display: flex; flex-direction: column; gap: 20px; }
.canvas { display: flex; flex-direction: column; gap: 24px; }

/* ── Panel ── */
.panel {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 22px;
  display: flex; flex-direction: column;
  gap: 18px;
}
.panel-header {
  display: flex; align-items: center; gap: 10px;
  font-size: 0.78rem; font-weight: 600;
  color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.06em;
}
.panel-header .num {
  width: 22px; height: 22px; border-radius: 50%;
  background: var(--surface); border: 1px solid var(--border);
  display: flex; align-items: center; justify-content: center;
  font-size: 0.7rem; font-weight: 700; color: var(--text-dim);
}

/* ── Dropzone ── */
.dropzone {
  position: relative; border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  background: var(--surface);
  padding: 36px 20px; text-align: center; cursor: pointer;
  transition: all 0.2s; overflow: hidden;
}
.dropzone::before {
  content: ''; position: absolute; inset: 0;
  background: radial-gradient(600px circle at var(--mx,50%) var(--my,50%), var(--accent-glow-strong), transparent 40%);
  opacity: 0; transition: opacity 0.4s; pointer-events: none;
}
.dropzone:hover::before, .dropzone.dragover::before { opacity: 1; }
.dropzone:hover, .dropzone.dragover {
  border-color: rgba(255,107,53,0.35); background: var(--surface-hover);
}
.dropzone input { position: absolute; inset: 0; opacity: 0; cursor: pointer; z-index: 2; }
.dropzone .icon { font-size: 1.4rem; margin-bottom: 10px; opacity: 0.7; }
.dropzone .label { font-size: 0.9rem; font-weight: 500; color: var(--text); }
.dropzone .hint { font-size: 0.78rem; color: var(--text-dim); margin-top: 5px; }
.dropzone .file-name { margin-top: 10px; font-size: 0.8rem; color: var(--text-secondary); word-break: break-all; }
.dropzone .remove {
  margin-top: 10px; font-size: 0.75rem; color: var(--danger); cursor: pointer;
  display: none; text-decoration: underline;
}

/* ── Preview in sidebar ── */
.sidebar-preview {
  display: none; border-radius: var(--radius-sm); overflow: hidden;
  border: 1px solid var(--border); background: var(--bg);
}
.sidebar-preview img { width: 100%; max-height: 200px; object-fit: cover; display: block; }

/* ── Prompt ── */
.prompt-box textarea {
  width: 100%; padding: 14px; border: 1px solid var(--border);
  border-radius: var(--radius-sm); background: var(--surface);
  color: var(--text); font-family: var(--font); font-size: 0.9rem;
  line-height: 1.5; resize: vertical; min-height: 90px; outline: none;
  transition: border-color 0.2s, box-shadow 0.2s;
}
.prompt-box textarea:focus {
  border-color: rgba(255,107,53,0.45);
  box-shadow: 0 0 0 3px var(--accent-glow);
}
.prompt-box .hint { font-size: 0.78rem; color: var(--text-dim); margin-top: 8px; }

/* ── Presets ── */
.preset-row { display: flex; gap: 8px; flex-wrap: wrap; }
.preset-chip {
  padding: 7px 14px; border-radius: 100px;
  border: 1px solid var(--border); background: var(--surface);
  color: var(--text-secondary); font-size: 0.78rem; font-weight: 500;
  cursor: pointer; transition: all 0.15s; user-select: none;
}
.preset-chip:hover { border-color: var(--border-strong); color: var(--text); background: var(--surface-hover); }
.preset-chip.active {
  border-color: var(--accent); background: var(--accent-glow); color: var(--accent);
}

/* ── Chips ── */
.chip-row { display: flex; gap: 8px; flex-wrap: wrap; }
.quality-chip, .aspect-chip {
  padding: 8px 16px; border-radius: var(--radius-xs);
  border: 1px solid var(--border); background: var(--surface);
  color: var(--text-secondary); font-size: 0.82rem; font-weight: 500;
  cursor: pointer; transition: all 0.15s; user-select: none;
}
.quality-chip:hover, .aspect-chip:hover { border-color: var(--border-strong); color: var(--text); background: var(--surface-hover); }
.quality-chip.active, .aspect-chip.active {
  border-color: var(--accent); background: var(--accent-glow); color: var(--accent);
}

/* ── Generate Button ── */
.gen-btn-wrap { margin-top: 4px; }
.gen-btn {
  width: 100%; padding: 16px 24px;
  border: none; border-radius: var(--radius-sm);
  background: var(--accent); color: #fff;
  font-family: var(--font); font-size: 0.95rem; font-weight: 600;
  cursor: pointer; transition: all 0.2s;
  display: flex; align-items: center; justify-content: center; gap: 10px;
  position: relative; overflow: hidden;
}
.gen-btn:hover { background: var(--accent-hover); transform: translateY(-1px); }
.gen-btn:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }
.gen-btn .spinner {
  width: 18px; height: 18px;
  border: 2px solid rgba(255,255,255,0.3);
  border-top-color: #fff; border-radius: 50%;
  animation: spin 0.8s linear infinite; display: none;
}
.gen-btn.generating .spinner { display: block; }
.gen-btn.generating .label { display: none; }
.gen-btn .meta {
  position: absolute; right: 16px; font-size: 0.75rem; font-weight: 500;
  color: rgba(255,255,255,0.7); opacity: 0; transition: opacity 0.2s;
}
.gen-btn:hover .meta { opacity: 1; }

@keyframes spin { to { transform: rotate(360deg); } }

/* ── Canvas output ── */
.output-stage {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  min-height: 320px;
  display: flex; flex-direction: column;
}
.output-stage .stage-header {
  padding: 14px 20px; border-bottom: 1px solid var(--border);
  display: flex; align-items: center; justify-content: space-between;
}
.output-stage .stage-title { font-size: 0.85rem; font-weight: 600; color: var(--text-secondary); }
.output-stage .stage-body {
  flex: 1; padding: 20px;
  display: flex; flex-direction: column; gap: 16px;
}

/* ── Output Grid ── */
.output-grid {
  display: grid; grid-template-columns: repeat(2, 1fr); gap: 14px;
}
.output-grid.single { grid-template-columns: 1fr; }
.output-cell {
  border-radius: var(--radius-sm); overflow: hidden;
  border: 1px solid var(--border); background: var(--bg);
  position: relative;
}
.output-cell img { width: 100%; height: 100%; object-fit: cover; display: block; }
/* Default + ratio classes */
.output-cell, .output-cell.ratio-1-1 { aspect-ratio: 1 / 1; }
.output-cell.ratio-4-3 { aspect-ratio: 4 / 3; }
.output-cell.ratio-16-9 { aspect-ratio: 16 / 9; }
.output-cell.ratio-9-16 { aspect-ratio: 9 / 16; }
.output-cell.ratio-2-3 { aspect-ratio: 2 / 3; }
.output-cell.ratio-4-5 { aspect-ratio: 4 / 5; }
.output-cell .cell-bar {
  position: absolute; bottom: 0; left: 0; right: 0;
  display: flex; align-items: center; justify-content: space-between;
  padding: 8px 10px; opacity: 0; transition: opacity 0.2s;
  background: linear-gradient(to top, rgba(0,0,0,0.65), transparent);
}
.output-cell:hover .cell-bar { opacity: 1; }
.output-cell .cell-bar .left { display: flex; align-items: center; gap: 6px; }
.output-cell .cell-bar .pill {
  padding: 3px 8px; border-radius: 100px;
  background: rgba(255,255,255,0.1); backdrop-filter: blur(6px);
  color: #fff; font-size: 0.68rem; font-weight: 500; border: 1px solid rgba(255,255,255,0.1);
}
.output-cell .cell-bar .pill.ratio { background: rgba(255,255,255,0.08); color: rgba(255,255,255,0.85); }
.output-cell .cell-bar .pill.cost { background: rgba(45,212,168,0.15); color: #4ade80; border-color: rgba(74,222,128,0.25); }
.output-cell .cell-bar .pill.model { background: rgba(96,165,250,0.12); color: #93bbfc; border-color: rgba(96,165,250,0.2); }
.output-cell .cell-bar .pill.dims { background: rgba(255,255,255,0.06); color: var(--text-dim); border-color: rgba(255,255,255,0.08); font-size: 0.62rem; }
.output-cell .cell-bar .right { display: flex; gap: 6px; }
.output-cell .cell-bar .right a, .output-cell .cell-bar .right span {
  padding: 5px 10px; border-radius: var(--radius-xs);
  background: rgba(255,255,255,0.1); backdrop-filter: blur(6px);
  color: #fff; font-size: 0.72rem; font-weight: 600; text-decoration: none;
  border: 1px solid rgba(255,255,255,0.12); cursor: pointer; transition: background 0.15s;
}
.output-cell .cell-bar .right a:hover, .output-cell .cell-bar .right span:hover { background: rgba(255,255,255,0.2); }

/* ── Skeleton placeholder ── */
.skeleton-cell {
  border-radius: var(--radius-sm); overflow: hidden;
  border: 1px solid var(--border); background: var(--bg);
  position: relative; aspect-ratio: 1 / 1;
}
.skeleton-cell.ratio-4-3 { aspect-ratio: 4 / 3; }
.skeleton-cell.ratio-16-9 { aspect-ratio: 16 / 9; }
.skeleton-cell.ratio-9-16 { aspect-ratio: 9 / 16; }
.skeleton-cell.ratio-2-3 { aspect-ratio: 2 / 3; }
.skeleton-cell.ratio-4-5 { aspect-ratio: 4 / 5; }
.skeleton-cell::after {
  content: ''; position: absolute; inset: 0;
  background: linear-gradient(90deg, transparent, rgba(255,255,255,0.04), transparent);
  background-size: 200% 100%;
  animation: shimmer 1.4s infinite;
}
@keyframes shimmer {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}

/* ── Empty state ── */
.empty-state {
  flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 14px; color: var(--text-dim); text-align: center; padding: 40px;
}
.empty-state .icon { font-size: 2.5rem; opacity: 0.3; }
.empty-state h3 { font-size: 1rem; font-weight: 600; color: var(--text-secondary); }
.empty-state p { font-size: 0.82rem; max-width: 300px; }

/* ── Gallery ── */
.gallery-panel {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 18px;
}
.gallery-panel .panel-title {
  font-size: 0.75rem; font-weight: 600; color: var(--text-secondary);
  text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 14px;
  display: flex; justify-content: space-between; align-items: center;
}
.gallery-toolbar {
  display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap;
}
.gallery-toolbar button {
  font-size: 0.72rem; padding: 4px 10px; border-radius: var(--radius-xs);
  border: 1px solid var(--border); background: var(--bg-hover);
  color: var(--text-secondary); cursor: pointer; font-family: var(--font);
  transition: all 0.15s;
}
.gallery-toolbar button:hover { border-color: var(--accent); color: var(--text); }
.gallery-toolbar button.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
.gallery-toolbar button.primary:hover { background: var(--accent-hover); }
.gallery-toolbar .count { margin-left: auto; font-size: 0.72rem; color: var(--text-dim); }
.gallery-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(80px, 1fr));
  gap: 8px;
}
.gallery-thumb {
  border-radius: var(--radius-xs); overflow: hidden;
  border: 1px solid var(--border); cursor: pointer;
  opacity: 0.65; transition: opacity 0.15s, border-color 0.15s; position: relative;
}
.gallery-thumb:hover, .gallery-thumb.active, .gallery-thumb.selected { opacity: 1; border-color: var(--accent); }
.gallery-thumb.selected::after {
  content: '';
  position: absolute; inset: 0; border: 2px solid var(--accent); border-radius: var(--radius-xs);
  pointer-events: none;
}
.gallery-thumb img { width: 100%; height: 80px; object-fit: cover; display: block; }
.gallery-thumb .check {
  position: absolute; top: 4px; left: 4px; width: 16px; height: 16px;
  background: rgba(0,0,0,0.5); border-radius: 3px; border: 1px solid rgba(255,255,255,0.3);
  display: flex; align-items: center; justify-content: center;
  font-size: 10px; color: #fff; opacity: 0; transition: opacity 0.15s;
}
.gallery-thumb:hover .check, .gallery-thumb.selected .check { opacity: 1; }
.gallery-thumb.selected .check { background: var(--accent); border-color: var(--accent); }
.gallery-thumb .del {
  position: absolute; top: 4px; right: 4px; width: 18px; height: 18px;
  background: rgba(0,0,0,0.5); color: #fff; border-radius: 50%;
  font-size: 11px; line-height: 18px; text-align: center; cursor: pointer;
  opacity: 0; transition: opacity 0.15s;
}
.gallery-thumb:hover .del { opacity: 1; }

/* ── Cost bar ── */
.cost-bar {
  display: flex; align-items: center; justify-content: center; gap: 16px;
  padding: 12px;
  font-size: 0.78rem; color: var(--text-dim);
}
.cost-bar input {
  width: 50px; background: transparent; color: var(--text);
  border: none; border-bottom: 1px solid var(--border);
  text-align: center; font-size: 0.78rem; font-family: var(--font);
  padding: 2px 4px;
}

/* ── Toast ── */
.toast {
  position: fixed; bottom: 24px; left: 50%;
  transform: translateX(-50%) translateY(20px);
  padding: 12px 24px; border-radius: var(--radius);
  font-size: 0.9rem; font-weight: 500;
  opacity: 0; pointer-events: none;
  transition: opacity 0.3s, transform 0.3s; z-index: 100;
}
.toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
.toast.ok { background: #1a3a2f; color: #4ade80; border: 1px solid rgba(74,222,128,0.2); }
.toast.err { background: #3a1a1a; color: #f87171; border: 1px solid rgba(248,113,113,0.2); }

/* ── Lightbox ── */
.lightbox-overlay {
  position: fixed; inset: 0; z-index: 200;
  background: rgba(0,0,0,0.92); backdrop-filter: blur(12px);
  display: none; align-items: center; justify-content: center;
}
.lightbox-overlay.active { display: flex; }
.lightbox-inner {
  position: relative; max-width: 90vw; max-height: 90vh;
  display: flex; flex-direction: column; align-items: center; gap: 16px;
}
.lightbox-img {
  max-width: 85vw; max-height: 75vh; object-fit: contain;
  border-radius: var(--radius-sm); border: 1px solid var(--border);
}
.lightbox-meta {
  display: flex; gap: 8px; align-items: center;
}
.lightbox-close {
  position: absolute; top: -40px; right: 0;
  width: 36px; height: 36px; border-radius: 50%;
  background: rgba(255,255,255,0.1); border: 1px solid rgba(255,255,255,0.15);
  color: #fff; font-size: 1.2rem; cursor: pointer; display: flex;
  align-items: center; justify-content: center; transition: background 0.15s;
}
.lightbox-close:hover { background: rgba(255,255,255,0.2); }
.lightbox-nav {
  position: absolute; top: 50%; transform: translateY(-50%);
  width: 44px; height: 44px; border-radius: 50%;
  background: rgba(255,255,255,0.1); border: 1px solid rgba(255,255,255,0.15);
  color: #fff; font-size: 1.2rem; cursor: pointer; display: flex;
  align-items: center; justify-content: center;
}
.lightbox-nav:hover { background: rgba(255,255,255,0.2); }
.lightbox-nav.prev { left: -60px; }
.lightbox-nav.next { right: -60px; }

/* ── Animations ── */
@keyframes fadeIn { from { opacity:0; transform: translateY(6px); } to { opacity:1; transform: translateY(0); } }
.fade-in { animation: fadeIn 0.35s ease forwards; }

/* ── Mobile ── */
@media (max-width: 960px) {
  .header-inner { padding: 0 16px; }
  .header-nav { display: none; }
  .main { padding: 20px 16px 60px; }
  .output-grid { grid-template-columns: 1fr; }
  .output-cell, .output-cell.ratio-1-1 { aspect-ratio: 1 / 1; }
}


/* ── Landing Sections ── */
.landing {
  border-top: 1px solid var(--border);
  padding-top: 80px;
}

/* Hero */
.hero-section {
  padding: 60px 24px;
  text-align: center;
}
.hero-inner { max-width: 720px; margin: 0 auto; }
.hero-section h1 {
  font-size: 2.6rem; font-weight: 700; letter-spacing: -0.02em;
  line-height: 1.15; margin-bottom: 18px;
}
.hero-section h1 span { color: var(--accent); }
.hero-section p {
  font-size: 1.05rem; color: var(--text-secondary); line-height: 1.6;
  max-width: 560px; margin: 0 auto 28px;
}
.hero-cta {
  display: flex; align-items: center; justify-content: center; gap: 16px; flex-wrap: wrap;
  margin-bottom: 48px;
}
.hero-btn {
  padding: 14px 28px; border-radius: var(--radius-sm);
  background: var(--accent); color: #fff; border: none;
  font-family: var(--font); font-size: 0.95rem; font-weight: 600;
  cursor: pointer; transition: all 0.2s; text-decoration: none; display: inline-block;
}
.hero-btn:hover { background: var(--accent-hover); transform: translateY(-1px); }
.hero-note { font-size: 0.82rem; color: var(--text-dim); }
.hero-stats {
  display: flex; justify-content: center; gap: 40px; flex-wrap: wrap;
}
.stat { display: flex; flex-direction: column; align-items: center; gap: 4px; }
.stat-num { font-size: 1.6rem; font-weight: 700; color: var(--accent); }
.stat-lbl { font-size: 0.78rem; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.06em; }

/* Trust bar */
.trust-bar { border-top: 1px solid var(--border); border-bottom: 1px solid var(--border); padding: 28px 24px; }
.trust-inner { max-width: 1100px; margin: 0 auto; display: flex; align-items: center; justify-content: center; gap: 28px; flex-wrap: wrap; }
.trust-label { font-size: 0.78rem; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.06em; }
.trust-logos { display: flex; gap: 20px; flex-wrap: wrap; justify-content: center; }
.trust-logo {
  padding: 8px 18px; border-radius: var(--radius-xs);
  border: 1px solid var(--border); background: var(--bg-elevated);
  font-size: 0.82rem; font-weight: 500; color: var(--text-secondary);
}

/* Features grid */
.features-grid {
  max-width: 1100px; margin: 0 auto;
  padding: 64px 24px;
  display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px;
}
@media (max-width: 960px) { .features-grid { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 600px) { .features-grid { grid-template-columns: 1fr; } }
.feature-card {
  background: var(--bg-elevated); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 28px;
  display: flex; flex-direction: column; gap: 12px;
  transition: border-color 0.2s;
}
.feature-card:hover { border-color: var(--border-strong); }
.feature-icon { font-size: 1.6rem; margin-bottom: 4px; }
.feature-card h3 { font-size: 0.95rem; font-weight: 600; }
.feature-card p { font-size: 0.85rem; color: var(--text-secondary); line-height: 1.55; }

/* How it works */
.how-section { padding: 64px 24px; border-top: 1px solid var(--border); }
.how-inner { max-width: 960px; margin: 0 auto; }
.how-inner h2 { text-align: center; font-size: 1.6rem; font-weight: 700; margin-bottom: 40px; }
.steps-row { display: grid; grid-template-columns: repeat(3, 1fr); gap: 32px; }
@media (max-width: 720px) { .steps-row { grid-template-columns: 1fr; } }
.step { display: flex; flex-direction: column; gap: 10px; }
.step-num {
  font-size: 2.2rem; font-weight: 700; color: var(--accent); opacity: 0.35;
  line-height: 1;
}
.step h4 { font-size: 0.95rem; font-weight: 600; margin-top: 4px; }
.step p { font-size: 0.85rem; color: var(--text-secondary); line-height: 1.55; }

/* Pricing */
.pricing-section { padding: 64px 24px; border-top: 1px solid var(--border); }
.pricing-inner { max-width: 900px; margin: 0 auto; text-align: center; }
.pricing-inner h2 { font-size: 1.6rem; font-weight: 700; margin-bottom: 8px; }
.pricing-sub { font-size: 0.9rem; color: var(--text-secondary); margin-bottom: 40px; }
.tiers { display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; align-items: start; }
@media (max-width: 720px) { .tiers { grid-template-columns: 1fr; max-width: 380px; margin: 0 auto; } }
.tier {
  background: var(--bg-elevated); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 28px; text-align: left;
  position: relative;
}
.tier-popular { border-color: rgba(255,107,53,0.35); background: var(--accent-glow); }
.tier-badge {
  position: absolute; top: -10px; left: 50%; transform: translateX(-50%);
  background: var(--accent); color: #fff; font-size: 0.7rem; font-weight: 600;
  padding: 4px 12px; border-radius: 100px; letter-spacing: 0.02em;
}
.tier-label { font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--text-secondary); font-weight: 600; margin-bottom: 8px;
}
.tier-price { font-size: 2.2rem; font-weight: 700; }
.tier-unit { font-size: 0.82rem; color: var(--text-secondary); margin-bottom: 20px; }
.tier-features { list-style: none; display: flex; flex-direction: column; gap: 8px; }
.tier-features li {
  font-size: 0.85rem; color: var(--text-secondary); padding-left: 18px;
  position: relative;
}
.tier-features li::before {
  content: ''; position: absolute; left: 0; top: 8px; width: 6px; height: 6px;
  border-radius: 50%; background: var(--accent); opacity: 0.6;
}

/* FAQ */
.faq-section { padding: 64px 24px; border-top: 1px solid var(--border); }
.faq-inner { max-width: 680px; margin: 0 auto; }
.faq-inner h2 { font-size: 1.6rem; font-weight: 700; margin-bottom: 28px; text-align: center; }
.faq-item { border-bottom: 1px solid var(--border); }
.faq-item summary {
  padding: 18px 0; font-size: 0.95rem; font-weight: 500; cursor: pointer;
  list-style: none; display: flex; justify-content: space-between; align-items: center;
}
.faq-item summary::after { content: '+'; font-size: 1.2rem; color: var(--text-dim); }
.faq-item[open] summary::after { content: '−'; }
.faq-item p { font-size: 0.85rem; color: var(--text-secondary); line-height: 1.6; padding-bottom: 18px; }

/* CTA Banner */
.cta-banner { padding: 64px 24px; text-align: center; border-top: 1px solid var(--border); }
.cta-inner { max-width: 560px; margin: 0 auto; }
.cta-banner h2 { font-size: 1.5rem; font-weight: 700; margin-bottom: 10px; }
.cta-banner p { font-size: 0.9rem; color: var(--text-secondary); margin-bottom: 24px; }

/* Footer */
.landing-footer { border-top: 1px solid var(--border); padding: 32px 24px; }
.footer-inner {
  max-width: 1100px; margin: 0 auto;
  display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 16px;
}
.footer-brand { display: flex; align-items: center; gap: 8px; font-weight: 600; font-size: 0.9rem; }
.footer-mark {
  width: 24px; height: 24px; background: var(--accent); border-radius: 6px;
  display: flex; align-items: center; justify-content: center;
  font-size: 11px; color: #fff; font-weight: 700;
}
.footer-links { display: flex; gap: 20px; }
.footer-links a { font-size: 0.82rem; color: var(--text-secondary); text-decoration: none; transition: color 0.15s; }
.footer-links a:hover { color: var(--text); }
.footer-copy { font-size: 0.78rem; color: var(--text-dim); }
@media (max-width: 600px) {
  .footer-inner { flex-direction: column; text-align: center; }
  .hero-section h1 { font-size: 1.9rem; }
}


/* ── API Key Panel ── */
.apikey-panel {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.apikey-panel .panel-header {
  display: flex; align-items: center; gap: 8px;
  font-size: 0.78rem; font-weight: 600; color: var(--text-secondary);
  text-transform: uppercase; letter-spacing: 0.06em;
}
.apikey-panel .panel-header .icon { font-size: 1rem; }
.apikey-input-wrap {
  position: relative;
  display: flex; gap: 8px;
}
.apikey-input-wrap input {
  flex: 1; padding: 10px 14px; border: 1px solid var(--border);
  border-radius: var(--radius-xs); background: var(--surface);
  color: var(--text); font-family: var(--font); font-size: 0.85rem;
  outline: none; transition: border-color 0.2s, box-shadow 0.2s;
}
.apikey-input-wrap input:focus {
  border-color: rgba(255,107,53,0.45); box-shadow: 0 0 0 3px var(--accent-glow);
}
.apikey-input-wrap input.valid { border-color: var(--success); }
.apikey-input-wrap input.invalid { border-color: var(--danger); }
.apikey-btn {
  padding: 9px 16px; border-radius: var(--radius-xs);
  border: 1px solid var(--border); background: var(--surface-hover);
  color: var(--text); font-family: var(--font); font-size: 0.82rem;
  font-weight: 600; cursor: pointer; transition: all 0.15s; white-space: nowrap;
}
.apikey-btn:hover { background: var(--surface); }
.apikey-hint {
  font-size: 0.75rem; color: var(--text-dim); line-height: 1.5;
}
.apikey-hint a { color: var(--accent); text-decoration: none; }
.apikey-hint a:hover { text-decoration: underline; }
.apikey-status {
  font-size: 0.78rem; font-weight: 500; display: none;
}
.apikey-status.ok { color: var(--success); display: block; }
.apikey-status.err { color: var(--danger); display: block; }

/* ── Prompt History ── */
.prompt-history {
  max-height: 200px; overflow-y: auto;
  display: flex; flex-direction: column; gap: 6px;
}
.prompt-history-item {
  padding: 8px 10px; border-radius: var(--radius-xs);
  background: var(--bg-hover); border: 1px solid var(--border);
  font-size: 0.82rem; color: var(--text-secondary);
  cursor: pointer; transition: all 0.15s;
  display: flex; align-items: center; gap: 8px;
  line-height: 1.3;
}
.prompt-history-item:hover {
  border-color: var(--accent); color: var(--text);
  background: rgba(255,107,53,0.05);
}
.prompt-history-item .prompt-text {
  flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.prompt-history-item .prompt-meta {
  font-size: 0.68rem; color: var(--text-dim); white-space: nowrap;
}
.prompt-history-empty {
  font-size: 0.78rem; color: var(--text-dim); padding: 8px 0;
}

</style>
</head>
<body>

<header class="header">
  <div class="header-inner">
    <a href="/" class="header-brand">
      <div class="mark">CS</div>
      <span class="name">Creative Studio</span>
    </a>
    <nav class="header-nav">
      <a href="/" class="active">Studio</a>
      <a href="/status">Status</a>
      <a href="/docs">API Docs</a>
      <a href="/history">History</a>
    </nav>
    <div class="credits-pill">
      <span>Today: <span class="amt" id="costToday">$0.00</span></span>
      <span class="sep">|</span>
      <span>Limit: $<input type="number" id="costLimit" value="5.00" step="0.50" min="0" style="width:44px;background:transparent;color:var(--text);border:none;border-bottom:1px solid var(--border);text-align:center;font-size:0.78rem;font-family:var(--font);padding:2px;"></span>
    </div>
  </div>
</header>

<main class="main" id="editor">

  <!-- Sidebar Controls -->
  <aside class="sidebar">

    <!-- 0. API Key (BYOK) -->
    <div class="apikey-panel" id="apikeyPanel">
      <div class="panel-header"><span class="icon">🔑</span> API Key</div>
      <div class="apikey-input-wrap">
        <input type="password" id="apikeyInput" placeholder="Paste your Gemini API key..." autocomplete="off">
        <button class="apikey-btn" id="apikeyBtn">Save</button>
      </div>
      <div class="apikey-status" id="apikeyStatus"></div>
      <div class="apikey-hint">
        Bring your own key from <a href="https://aistudio.google.com/app/apikey" target="_blank">Google AI Studio</a>.
        We don't store or train on your key. Cost is paid directly to Google.
      </div>
    </div>

    <!-- 1. Product -->
    <div class="panel">
      <div class="panel-header"><span class="num">1</span> Your Product</div>
      <div class="dropzone" id="dropzone">
        <input type="file" id="fileInput" accept="image/*">
        <div class="icon">&#128248;</div>
        <div class="label">Drop product photo here</div>
        <div class="hint">PNG, JPG, WEBP</div>
        <div class="file-name" id="fileName"></div>
        <div class="remove" id="removeBtn">Remove</div>
      </div>
      <div class="sidebar-preview" id="previewWrap">
        <img id="previewImg" alt="Product preview">
      </div>
    </div>

    <!-- 2. Scene -->
    <div class="panel">
      <div class="panel-header"><span class="num">2</span> Scene</div>
      <div class="prompt-box">
        <textarea id="prompt" placeholder="e.g. Premium protein tub on a clean oak shelf in a boutique fitness store, warm overhead lighting, shallow depth of field, product photography style"></textarea>
        <div class="preset-row" id="presetRow">
          <div class="preset-chip" data-preset="amazon">Amazon white</div>
          <div class="preset-chip" data-preset="instagram">Instagram</div>
          <div class="preset-chip" data-preset="email">Email banner</div>
          <div class="preset-chip" data-preset="pinterest">Pinterest</div>
        </div>
        <div class="hint">Be specific about setting, lighting, and mood.</div>
      </div>
    </div>

    <!-- Prompt History -->
    <div class="panel" id="promptHistoryPanel" style="display:none;">
      <div class="panel-header"><span class="num" style="background:var(--accent);color:#fff;">↺</span> Prompt History</div>
      <div class="prompt-history" id="promptHistory"></div>
    </div>

    <!-- 3. Aspect Ratio -->
    <div class="panel">
      <div class="panel-header"><span class="num">3</span> Aspect Ratio</div>
      <div class="chip-row" id="aspectRow">
        <div class="aspect-chip active" data-ratio="1:1">1:1</div>
        <div class="aspect-chip" data-ratio="4:3">4:3</div>
        <div class="aspect-chip" data-ratio="16:9">16:9</div>
        <div class="aspect-chip" data-ratio="9:16">9:16</div>
        <div class="aspect-chip" data-ratio="2:3">2:3</div>
        <div class="aspect-chip" data-ratio="4:5">4:5</div>
      </div>
    </div>

    <!-- 4. Quality -->
    <div class="panel">
      <div class="panel-header"><span class="num">4</span> Quality</div>
      <div class="chip-row" id="qualityRow">
        <div class="quality-chip active" data-tier="fast" data-cost="0.07">Fast &middot; $0.07</div>
        <div class="quality-chip" data-tier="balanced" data-cost="0.07">Balanced &middot; $0.07</div>
        <div class="quality-chip" data-tier="quality" data-cost="0.20">Quality &middot; $0.20</div>
      </div>
      <label style="display:flex;align-items:center;gap:8px;font-size:0.82rem;color:var(--text-dim);cursor:pointer;margin-top:4px;">
        <input type="checkbox" id="batchToggle" style="accent-color:var(--accent);">
        Generate 4 variations (slower)
      </label>
    </div>

    <!-- Generate -->
    <div class="gen-btn-wrap">
      <button class="gen-btn" id="genBtn">
        <div class="spinner"></div>
        <span class="label" id="genLabel">Generate Image</span>
        <span class="meta" id="genMeta"></span>
      </button>
    </div>

  </aside>

  <!-- Canvas -->
  <section class="canvas">

    <!-- Output Stage -->
    <div class="output-stage">
      <div class="stage-header">
        <span class="stage-title">Output</span>
        <div style="display:flex;align-items:center;gap:10px;">
          <button id="downloadAllBtn" style="display:none;padding:4px 10px;border-radius:var(--radius-xs);border:1px solid var(--border);background:var(--surface);color:var(--text-secondary);font-family:var(--font);font-size:0.72rem;cursor:pointer;">Download all</button>
          <span class="stage-title" id="outputMeta" style="font-weight:500;"></span>
        </div>
      </div>
      <div class="stage-body" id="stageBody">
        <div class="empty-state" id="emptyState">
          <div class="icon">&#127912;</div>
          <h3>No images yet</h3>
          <p>Configure your shot on the left and hit Generate to create product photography</p>
        </div>
        <div class="output-grid" id="outputGrid" style="display:none;"></div>
      </div>
    </div>

    <!-- Gallery -->
    <div class="gallery-panel" id="galleryCard" style="display:none;">
      <div class="panel-title">
        <span>Session Gallery</span>
        <span style="font-size:0.75rem;color:var(--text-dim);cursor:pointer;" id="clearGallery">Clear all</span>
      </div>
      <div class="gallery-toolbar" id="galleryToolbar" style="display:none;">
        <button id="selectAllBtn">Select all</button>
        <button id="deselectAllBtn">Deselect</button>
        <button id="downloadZipBtn" class="primary">Download ZIP</button>
        <button id="deleteSelectedBtn" style="color:var(--danger)">Delete</button>
        <span class="count" id="selectedCount">0 selected</span>
      </div>
      <div class="gallery-grid" id="gallery"></div>
    </div>

  </section>

</main>

<!-- ═══════ LANDING / COMMERCIAL CONTENT ═══════ -->
<section class="landing" id="landing">

  <!-- Hero pitch (shown when not logged in) -->
  <div class="hero-section" id="heroSection">
    <div class="hero-inner">
      <h1>AI product photography for <span>DTC brands</span> that ship</h1>
      <p>Upload your product. Describe the scene. Get studio-grade shots in seconds. Bring your own Gemini API key. Pay Google directly. No credit card, no subscription, no retouching.</p>
      <div class="hero-cta">
        <button class="hero-btn" onclick="document.getElementById('editor').scrollIntoView({behavior:'smooth'})">Try free →</button>
        <span class="hero-note">$0.07/image when you bring your own API key.</span>
      </div>
      <div class="hero-stats">
        <div class="stat"><span class="stat-num">30s</span><span class="stat-lbl">per shot</span></div>
        <div class="stat"><span class="stat-num">$0.07</span><span class="stat-lbl">starting cost</span></div>
        <div class="stat"><span class="stat-num">6</span><span class="stat-lbl">aspect ratios</span></div>
        <div class="stat"><span class="stat-num">4</span><span class="stat-lbl">platform presets</span></div>
      </div>
    </div>
  </div>

  <!-- Trusted by (logos) -->
  <div class="trust-bar">
    <div class="trust-inner">
      <span class="trust-label">Built for teams that need volume, speed, and consistency:</span>
      <div class="trust-logos">
        <div class="trust-logo">CPG Brand</div>
        <div class="trust-logo">Shopify Store</div>
        <div class="trust-logo">Design Agency</div>
        <div class="trust-logo">Amazon Seller</div>
        <div class="trust-logo">Pinterest Shop</div>
      </div>
    </div>
  </div>

  <!-- Features Grid -->
  <div class="features-grid">
    <div class="feature-card">
      <div class="feature-icon">📸</div>
      <h3>Product Compositing</h3>
      <p>Upload your real product photo. The AI places it into any scene you describe — white background, lifestyle flatlay, or hero banner.</p>
    </div>
    <div class="feature-card">
      <div class="feature-icon">🎨</div>
      <h3>Platform Presets</h3>
      <p>One click sets the optimal prompt + aspect ratio for Amazon, Instagram, Pinterest, or email banners.</p>
    </div>
    <div class="feature-card">
      <div class="feature-icon">⚡</div>
      <h3>Batch 4-Up</h3>
      <p>Generate 4 variations at once. Async polling means you can leave the tab and come back when your grid is ready.</p>
    </div>
    <div class="feature-card">
      <div class="feature-icon">💰</div>
      <h3>Pay-Per-Image</h3>
      <p>No subscription. No minimums. Fast tier at $0.07, Quality at $0.20. Cost guardrail keeps your team on budget.</p>
    </div>
    <div class="feature-card">
      <div class="feature-icon">🔒</div>
      <h3>Private Generation</h3>
      <p>Everything runs on your own Coolify instance. Your products, prompts, and outputs never touch a third-party AI gallery.</p>
    </div>
    <div class="feature-card">
      <div class="feature-icon">📱</div>
      <h3>6 Aspect Ratios</h3>
      <p>1:1 for feeds, 16:9 for banners, 9:16 for Stories, 4:5 for Pinterest, 4:3 for catalog, 2:3 for editorial.</p>
    </div>
  </div>

  <!-- How it works -->
  <div class="how-section">
    <div class="how-inner">
      <h2>How it works</h2>
      <div class="steps-row">
        <div class="step">
          <div class="step-num">01</div>
          <h4>Upload product</h4>
          <p>Drag your product photo into the dropzone. PNG, JPG, or WEBP. The AI keeps your exact packaging.</p>
        </div>
        <div class="step">
          <div class="step-num">02</div>
          <h4>Describe the scene</h4>
          <p>Type what you want — or click a preset. "Clean white background, soft shadow, studio lighting."</p>
        </div>
        <div class="step">
          <div class="step-num">03</div>
          <h4>Download &amp; ship</h4>
          <p>Single image in ~30s. Batch 4-up in ~2 min. Download PNGs and drop them straight into your storefront.</p>
        </div>
      </div>
    </div>
  </div>

  <!-- Pricing -->
  <div class="pricing-section" id="pricing">
    <div class="pricing-inner">
      <h2>Simple pricing</h2>
      <p class="pricing-sub">No subscriptions. No minimums. Pay only for what you generate.</p>
      <div class="tiers">
        <div class="tier">
          <div class="tier-label">Fast</div>
          <div class="tier-price">$0.07</div>
          <div class="tier-unit">per image</div>
          <ul class="tier-features">
            <li>Draft quality — great for mocks &amp; variants</li>
            <li>1K resolution</li>
            <li>~30 seconds</li>
            <li>All 6 aspect ratios</li>
          </ul>
        </div>
        <div class="tier tier-popular">
          <div class="tier-badge">Most popular</div>
          <div class="tier-label">Balanced</div>
          <div class="tier-price">$0.07</div>
          <div class="tier-unit">per image</div>
          <ul class="tier-features">
            <li>2K output with better lighting</li>
            <li>Same cost as Fast</li>
            <li>~30 seconds</li>
            <li>Recommended for most shots</li>
          </ul>
        </div>
        <div class="tier">
          <div class="tier-label">Quality</div>
          <div class="tier-price">$0.20</div>
          <div class="tier-unit">per image</div>
          <ul class="tier-features">
            <li>Pro-grade detail &amp; depth</li>
            <li>2K resolution</li>
            <li>Best for hero shots</li>
            <li>Product compositing</li>
          </ul>
        </div>
      </div>
    </div>
  </div>

  <!-- FAQ -->
  <div class="faq-section" id="faq">
    <div class="faq-inner">
      <h2>FAQ</h2>
      <div class="faq-list">
        <details class="faq-item">
          <summary>What makes this different from Midjourney or DALL-E?</summary>
          <p>Creative Studio is built for product photography workflows. It composites your real product into AI-generated scenes, maintains packaging accuracy, and outputs in exact platform aspect ratios. No prompt engineering required — presets handle the setup.</p>
        </details>
        <details class="faq-item">
          <summary>Who is this for?</summary>
          <p>DTC founders, CPG brands with monthly content calendars, Shopify operators, Amazon sellers needing white-background shots, and design agencies that need volume fast. Anyone who needs photos but doesn't have $500/day for a studio.</p>
        </details>
        <details class="faq-item">
          <summary>Do you store my API key or credit card?</summary>
          <p>No. Your API key lives in your browser's localStorage — we never see it on our server unless you pass it in the header. No credit card required. You pay Google directly for generation.</p>
        </details>
        <details class="faq-item">
          <summary>What happens to my product photos?</summary>
          <p>They stay on your own server. We don't train on them, sell them, or use them for model improvement. You control the instance, the outputs, and the data.</p>
        </details>
        <details class="faq-item">
          <summary>Do you store credit card info?</summary>
          <p>No credit card required. You bring your own Gemini API key and pay Google directly. We only track per-image cost to help you budget.</p>
        </details>
      </div>
    </div>
  </div>

  <!-- CTA Banner -->
  <div class="cta-banner">
    <div class="cta-inner">
      <h2>Ready to replace your product photographer?</h2>
      <p>Start generating in the editor above. Paste your Gemini key and go.</p>
      <button class="hero-btn" onclick="document.getElementById('editor').scrollIntoView({behavior:'smooth'})">Open Studio →</button>
    </div>
  </div>

</section>

<!-- ═══════ FOOTER ═══════ -->
<footer class="landing-footer">
  <div class="footer-inner">
    <div class="footer-brand">
      <div class="footer-mark">CS</div>
      <span>Creative Studio</span>
    </div>
    <div class="footer-links">
      <a href="/status">Status</a>
      <a href="#pricing">Pricing</a>
      <a href="#faq">FAQ</a>
      <a href="mailto:hello@ashbi.ca">Contact</a>
    </div>
    <div class="footer-copy">Built by Ashbi Design. Self-hosted on your own infrastructure.</div>
  </div>
</footer>


<div class="toast" id="toast"></div>

<script>
const $ = id = document.getElementById(id);
let state = { tier: 'fast', aspect: '1:1', prodImage: null, generating: false, gallery: [], selected: new Set(), lastClicked: null, outputImages: [], lastPrompt: '' };

// ── API Key (BYOK) ──
const API_KEY_STORAGE = 'cs_api_key';
function loadApiKey() { return localStorage.getItem(API_KEY_STORAGE) || ''; }
function saveApiKey(key) { localStorage.setItem(API_KEY_STORAGE, key); }
function getApiKey() { return loadApiKey(); }

function updateFetchOptions(opts = {}) {
  const key = getApiKey();
  if (key) {
    opts.headers = opts.headers || {};
    opts.headers['X-API-Key'] = key;
  }
  return opts;
}

async function validateApiKey(key) {
  try {
    const r = await fetch('/api/validate-key', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key })
    });
    return await r.json();
  } catch (e) { return { valid: false, error: e.message }; }
}

$('apikeyBtn').addEventListener('click', async () => {
  const key = $('apikeyInput').value.trim();
  if (!key) { showToast('Paste a key first', 'err'); return; }
  $('apikeyStatus').textContent = 'Checking...';
  $('apikeyStatus').className = 'apikey-status';
  const result = await validateApiKey(key);
  if (result.valid) {
    saveApiKey(key);
    $('apikeyStatus').textContent = 'Key saved ✓';
    $('apikeyStatus').className = 'apikey-status ok';
    $('apikeyInput').className = 'valid';
    showToast('API key saved', 'ok');
  } else {
    $('apikeyStatus').textContent = result.error || 'Invalid key';
    $('apikeyStatus').className = 'apikey-status err';
    $('apikeyInput').className = 'invalid';
  }
});

// Load saved key on startup
const savedKey = loadApiKey();
if (savedKey) {
  $('apikeyInput').value = savedKey;
  $('apikeyInput').className = 'valid';
  $('apikeyStatus').textContent = 'Key loaded ✓';
  $('apikeyStatus').className = 'apikey-status ok';
}

const PRESETS = {
  amazon:    { prompt: 'Clean pure white background, soft shadow underneath, studio lighting, product centered, ecommerce photography, high detail', aspect: '1:1' },
  instagram: { prompt: 'Lifestyle flatlay on textured surface, natural soft window light from left, shallow depth of field, lifestyle product photography', aspect: '1:1' },
  email:     { prompt: 'Product on clean gradient background, dramatic side lighting, hero shot, wide composition', aspect: '16:9' },
  pinterest: { prompt: 'Product in styled scene with complementary props, warm golden tones, overhead 45 degree angle, editorial style', aspect: '4:5' },
};

// ── Chip selectors ──
function initChips(rowId, key, cls) {
  $(rowId).addEventListener('click', e =\u003e {
    const chip = e.target.closest('.' + cls);
    if (!chip) return;
    document.querySelectorAll('#' + rowId + ' .' + cls).forEach(c =\u003e c.classList.remove('active'));
    chip.classList.add('active');
    state[key] = chip.dataset.tier || chip.dataset.ratio || chip.dataset.preset;
  });
}
initChips('qualityRow', 'tier', 'quality-chip');

// Aspect chips (separate to avoid initChips eating preset clicks)
$('aspectRow').addEventListener('click', e =\u003e {
  const chip = e.target.closest('.aspect-chip');
  if (!chip) return;
  document.querySelectorAll('#aspectRow .aspect-chip').forEach(c =\u003e c.classList.remove('active'));
  chip.classList.add('active');
  state.aspect = chip.dataset.ratio;
});

// ── Presets ──
$('presetRow').addEventListener('click', e =\u003e {
  const chip = e.target.closest('.preset-chip');
  if (!chip) return;
  const key = chip.dataset.preset;
  const p = PRESETS[key];
  if (!p) return;
  $('prompt').value = p.prompt;
  state.aspect = p.aspect;
  document.querySelectorAll('.aspect-chip').forEach(c =\u003e {
    c.classList.toggle('active', c.dataset.ratio === p.aspect);
  });
  document.querySelectorAll('.preset-chip').forEach(c =\u003e c.classList.remove('active'));
  chip.classList.add('active');
});

// ── Dropzone with mouse glow ──
const dz = $('dropzone'), fi = $('fileInput');
dz.addEventListener('mousemove', e =\u003e {
  const rect = dz.getBoundingClientRect();
  dz.style.setProperty('--mx', ((e.clientX - rect.left) / rect.width * 100) + '%');
  dz.style.setProperty('--my', ((e.clientY - rect.top) / rect.height * 100) + '%');
});

const onFile = file =\u003e {
  if (!file || !file.type.startsWith('image/')) return;
  state.prodImage = file;
  $('fileName').textContent = file.name;
  const url = URL.createObjectURL(file);
  $('previewImg').src = url;
  $('previewWrap').style.display = 'block';
  $('removeBtn').style.display = 'inline-block';
  dz.querySelector('.label').textContent = 'Replace product photo';
  dz.querySelector('.icon').textContent = '🔄';
  updateGenLabel();
};
fi.addEventListener('change', e =\u003e onFile(e.target.files[0]));
dz.addEventListener('dragover', e =\u003e { e.preventDefault(); dz.classList.add('dragover'); });
dz.addEventListener('dragleave', () =\u003e dz.classList.remove('dragover'));
dz.addEventListener('drop', e =\u003e {
  e.preventDefault(); dz.classList.remove('dragover');
  onFile(e.dataTransfer.files[0]);
});

$('removeBtn').addEventListener('click', () =\u003e {
  state.prodImage = null;
  $('fileName').textContent = '';
  $('previewWrap').style.display = 'none';
  $('removeBtn').style.display = 'none';
  fi.value = '';
  dz.querySelector('.label').textContent = 'Drop product photo here';
  dz.querySelector('.icon').textContent = '📸';
  updateGenLabel();
});

function updateGenLabel() {
  const batch = $('batchToggle').checked;
  const count = state.prodImage ? 1 : (batch ? 4 : 1);
  const label = state.prodImage ? 'Generate Composite' : (batch ? 'Generate 4 Images' : 'Generate Image');
  $('genLabel').textContent = label;
  const cost = state.prodImage ? 0.20 : (0.07 * count);
  const time = batch && !state.prodImage ? '~2 min' : '~30s';
  $('genMeta').textContent = '$' + cost.toFixed(2) + ' · ' + time;
}
$('batchToggle').addEventListener('change', updateGenLabel);

async function getCostToday() {
  try {
    const r = await fetch('/api/costs', updateFetchOptions());
    const d = await r.json();
    return d.today || 0;
  } catch (e) { return 0; }
}

function addToGallery(images) {
  images.forEach(img => state.gallery.push(img));
  renderGallery();
}

function renderGallery() {
  const g = $('gallery');
  g.innerHTML = '';
  state.gallery.forEach((img, idx) => {
    const thumb = document.createElement('div');
    const isSel = state.selected.has(idx);
    thumb.className = 'gallery-thumb' + (isSel ? ' selected' : '');
    thumb.innerHTML = '<img src="' + img.url + '" alt=""><div class="check">' + (isSel ? '✓' : '') + '</div><div class="del" data-idx="' + idx + '">×</div>';
    thumb.querySelector('.del').addEventListener('click', (e) => {
      e.stopPropagation();
      state.gallery.splice(idx, 1);
      state.selected.delete(idx);
      const newSelected = new Set();
      state.selected.forEach(i => { if (i < idx) newSelected.add(i); else if (i > idx) newSelected.add(i - 1); });
      state.selected = newSelected;
      renderGallery();
      updateToolbar();
    });
    thumb.addEventListener('click', (e) => {
      if (e.shiftKey && state.lastClicked !== null) {
        const start = Math.min(state.lastClicked, idx);
        const end = Math.max(state.lastClicked, idx);
        for (let i = start; i <= end; i++) state.selected.add(i);
      } else {
        if (state.selected.has(idx)) state.selected.delete(idx);
        else state.selected.add(idx);
        state.lastClicked = idx;
      }
      renderGallery();
      updateToolbar();
    });
    g.appendChild(thumb);
  });
  $('galleryCard').style.display = state.gallery.length > 0 ? 'block' : 'none';
  updateToolbar();
}

function updateToolbar() {
  const hasSel = state.selected.size > 0;
  $('galleryToolbar').style.display = state.gallery.length > 0 ? 'flex' : 'none';
  $('selectedCount').textContent = state.selected.size + ' selected';
  $('downloadZipBtn').disabled = !hasSel;
  $('deleteSelectedBtn').disabled = !hasSel;
}

$('selectAllBtn').addEventListener('click', () => {
  state.gallery.forEach((_, i) => state.selected.add(i));
  renderGallery();
});
$('deselectAllBtn').addEventListener('click', () => {
  state.selected.clear();
  renderGallery();
});
$('deleteSelectedBtn').addEventListener('click', () => {
  if (!state.selected.size) return;
  const remaining = state.gallery.filter((_, i) => !state.selected.has(i));
  state.gallery = remaining;
  state.selected.clear();
  renderGallery();
  showToast('Deleted selected images', 'ok');
});
$('downloadZipBtn').addEventListener('click', async () => {
  if (!state.selected.size) { showToast('Select images first', 'err'); return; }
  const urls = [];
  state.selected.forEach(i => { if (state.gallery[i]) urls.push(state.gallery[i].url); });
  try {
    const r = await fetch('/api/export-zip', updateFetchOptions({
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({urls})
    }));
    if (!r.ok) throw new Error('ZIP failed');
    const blob = await r.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'creative-studio-export.zip';
    a.click();
    showToast('ZIP downloaded', 'ok');
  } catch (e) { showToast('Export failed: ' + e.message, 'err'); }
});

async function loadServerGallery() {
  try {
    const r = await fetch('/api/sessions');
    const d = await r.json();
    if (d.sessions) {
      d.sessions.forEach(s => {
        (s.entries || []).forEach(e => {
          if (e.image_url && !state.gallery.find(g => g.url === e.image_url)) {
            state.gallery.push({ url: e.image_url, name: e.note || 'image.png', cost: e.cost || 0, model: e.model || '' });
          }
        });
      });
      renderGallery();
    }
  } catch (e) { console.log('session load failed', e); }
}
loadServerGallery();

// Delegate click for Save-to-gallery + Copy-prompt on output cells
$('outputGrid').addEventListener('click', (e) => {
  const btn = e.target.closest('.add-to-gallery');
  if (btn) {
    const url = btn.dataset.url;
    const name = btn.dataset.name;
    const cost = parseFloat(btn.dataset.cost) || 0;
    const model = btn.dataset.model || '';
    if (!state.gallery.find(g => g.url === url)) {
      state.gallery.push({ url, name, cost, model, ratio: state.aspect });
      renderGallery();
      showToast('Saved to gallery', 'ok');
    } else {
      showToast('Already in gallery', 'err');
    }
    return;
  }
  const copyBtn = e.target.closest('.copy-prompt');
  if (copyBtn) {
    const prompt = decodeURIComponent(copyBtn.dataset.prompt || '');
    if (prompt) {
      navigator.clipboard.writeText(prompt).then(() => showToast('Prompt copied', 'ok')).catch(() => showToast('Copy failed', 'err'));
    }
  }
});

function loadIntoOutput(images) {
  const grid = $('outputGrid');
  grid.innerHTML = '';
  grid.style.display = 'grid';
  grid.className = 'output-grid' + (images.length === 1 ? ' single' : '');
  $('emptyState').style.display = 'none';
  state.outputImages = images.slice();

  images.forEach((img, i) => {
    const cell = document.createElement('div');
    const ratioClass = (img.ratio || state.aspect || '1:1').replace(':', '-');
    cell.className = 'output-cell fade-in ratio-' + ratioClass;
    cell.style.animationDelay = (i * 0.08) + 's';
    cell.innerHTML = buildCellHTML(img);
    grid.appendChild(cell);
  });

  const totalCost = images.reduce((s, img) => s + (img.cost || 0), 0);
  $('outputMeta').textContent = images.length + ' image' + (images.length > 1 ? 's' : '') + ' · $' + totalCost.toFixed(2);
  $('downloadAllBtn').style.display = images.length > 0 ? 'inline-block' : 'none';
}

function appendToOutput(images) {
  const grid = $('outputGrid');
  grid.style.display = 'grid';
  $('emptyState').style.display = 'none';
  images.forEach(img => state.outputImages.push(img));

  images.forEach((img, i) => {
    const cell = document.createElement('div');
    const ratioClass = (img.ratio || state.aspect || '1:1').replace(':', '-');
    cell.className = 'output-cell fade-in ratio-' + ratioClass;
    cell.style.animationDelay = (i * 0.08) + 's';
    cell.innerHTML = buildCellHTML(img);
    grid.appendChild(cell);
  });

  const allCells = grid.querySelectorAll('.output-cell');
  const totalCost = images.reduce((s, img) => s + (img.cost || 0), 0);
  $('outputMeta').textContent = allCells.length + ' images · streaming...';
  $('downloadAllBtn').style.display = 'inline-block';
}

function dimBadge(ratio) {
  const map = { '1:1': '1024×1024', '4:3': '1024×768', '16:9': '1024×576', '9:16': '576×1024', '2:3': '683×1024', '4:5': '819×1024' };
  return map[ratio] || '';
}

function buildCellHTML(img) {
  const ratio = img.ratio || '';
  const cost = img.cost ? '$' + img.cost.toFixed(2) : '';
  const model = img.model ? img.model.replace('gemini-3.1-flash-image-preview', 'Flash').replace('gemini-3-pro-image-preview', 'Pro') : '';
  const prompt = img.prompt || state.lastPrompt || '';
  const dims = ratio ? dimBadge(ratio) : '';
  return (
    '<img src="' + img.url + '" alt="" data-prompt="' + encodeURIComponent(prompt) + '">' +
    '<div class="cell-bar">' +
      '<div class="left">' +
        (ratio ? '<span class="pill ratio">' + ratio + '</span>' : '') +
        (cost ? '<span class="pill cost">' + cost + '</span>' : '') +
        (model ? '<span class="pill model">' + model + '</span>' : '') +
        (dims ? '<span class="pill dims">' + dims + '</span>' : '') +
      '</div>' +
      '<div class="right">' +
        '<span class="copy-prompt" data-prompt="' + encodeURIComponent(prompt) + '" title="Copy prompt">📋</span>' +
        '<a href="' + img.url + '" download="' + img.name + '">Download</a>' +
        '<span class="add-to-gallery" data-url="' + img.url + '" data-name="' + img.name + '" data-cost="' + (img.cost||0) + '" data-model="' + (img.model||'') + '">Save</span>' +
      '</div>' +
    '</div>'
  );
}

// ── Generate ──
$('genBtn').addEventListener('click', async () =\u003e {
  const prompt = $('prompt').value.trim();
  if (!prompt) { showToast('Enter a scene description', 'err'); return; }
  if (state.generating) return;
  state.lastPrompt = prompt;

  const limit = parseFloat($('costLimit').value) || 5;
  if (limit < 0 || isNaN(limit)) { showToast('Invalid cost limit', 'err'); return; }
  const costToday = await getCostToday();
  const batch = $('batchToggle').checked;
  const count = state.prodImage ? 1 : (batch ? 4 : 1);
  const est = state.prodImage ? 0.20 : (0.07 * count);
  if (costToday + est > limit) {
    showToast('Would exceed $' + limit.toFixed(2) + ' cost limit', 'err');
    return;
  }

  state.generating = true;
  $('genBtn').disabled = true;
  $('genBtn').classList.add('generating');
  $('outputMeta').textContent = '';

  // Show skeleton placeholders
  const grid = $('outputGrid');
  grid.innerHTML = '';
  grid.style.display = 'grid';
  grid.className = 'output-grid' + (count === 1 ? ' single' : '');
  $('emptyState').style.display = 'none';
  for (let i = 0; i < count; i++) {
    const sk = document.createElement('div');
    sk.className = 'skeleton-cell ratio-' + (state.aspect || '1:1').replace(':', '-');
    grid.appendChild(sk);
  }

  try {
    let data;
    if (state.prodImage) {
      const fd = new FormData();
      fd.append('prompt', prompt);
      fd.append('product', state.prodImage);
      fd.append('aspect_ratio', state.aspect);
      fd.append('tier', state.tier);
      const resp = await fetch('/api/composite', updateFetchOptions({ method: 'POST', body: fd }));
      data = await resp.json();
    } else {
      const body = {
        prompt, mode: 'direct', tier: state.tier,
        aspect_ratio: state.aspect, variations: count
      };
      const resp = await fetch('/api/generate', updateFetchOptions({
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      }));
      data = await resp.json();
    }

    if (data.error) {
      showToast(data.error, 'err');
      return;
    }

    let streamed = false;
    if (data.job_id && data.status === 'running') {
      showToast('Batch started — this takes ~2 minutes', 'ok');
      const result = await pollJob(data.job_id, count);
      if (result.error) { showToast(result.error, 'err'); return; }
      data = result;
      streamed = true;
    }

    if (data.images && data.images.length) {
      if (!streamed) {
        loadIntoOutput(data.images);
        addToGallery(data.images);
      } else if (data.partial) {
        const grid3 = $('outputGrid');
        const retry = document.createElement('div');
        retry.className = 'output-cell';
        retry.style.display = 'flex'; retry.style.alignItems = 'center'; retry.style.justifyContent = 'center';
        retry.style.flexDirection = 'column'; retry.style.gap = '8px'; retry.style.color = 'var(--text-dim)';
        retry.innerHTML = '<div style="font-size:0.85rem;font-weight:600;">' + data.got + '/' + data.expected + ' generated</div><button id="retryBtn" style="padding:6px 14px;border-radius:var(--radius-xs);border:1px solid var(--border);background:var(--surface);color:var(--text);font-family:var(--font);font-size:0.78rem;cursor:pointer;">Retry missing</button>';
        grid3.appendChild(retry);
        $('retryBtn').addEventListener('click', () => {
          $('genBtn').click();
        });
        showToast(data.message, 'ok');
        $('downloadAllBtn').style.display = 'inline-block';
      } else {
        showToast(data.message || 'Done!', 'ok');
        $('downloadAllBtn').style.display = 'inline-block';
      }
      refreshCost();
    } else {
      showToast('No images returned', 'err');
    }
  } catch (e) {
    showToast('Network error: ' + e.message, 'err');
  } finally {
    state.generating = false;
    $('genBtn').disabled = false;
    $('genBtn').classList.remove('generating');
  }
});

async function pollJob(jobId, expectedCount) {
  const maxWait = 300;
  const interval = 4;
  const start = Date.now();
  let dots = 0;
  let streamedCount = 0;

  while (true) {
    const elapsed = (Date.now() - start) / 1000;
    if (elapsed > maxWait) {
      return { error: 'Timed out waiting for batch generation' };
    }
    dots = (dots + 1) % 4;
    $('genLabel').textContent = 'Generating' + '.'.repeat(dots) + ' (' + Math.round(elapsed) + 's)';

    await new Promise(r => setTimeout(r, interval * 1000));
    const r = await fetch('/api/jobs/' + jobId);
    const d = await r.json();

    // Stream partial results as they arrive
    if (d.partial && d.partial.images && d.partial.images.length > streamedCount) {
      const newImages = d.partial.images.slice(streamedCount);
      streamedCount = d.partial.images.length;
      // Remove skeleton placeholders, then append real images
      const grid2 = $('outputGrid');
      const skeletons = grid2.querySelectorAll('.skeleton-cell');
      skeletons.forEach((sk, idx) => {
        if (idx < streamedCount) sk.remove();
      });
      appendToOutput(newImages);
      addToGallery(newImages);
      $('genLabel').textContent = 'Generating ' + streamedCount + '/' + expectedCount + '...';
    }

    if (d.status === 'done') {
      $('genLabel').textContent = state.prodImage ? 'Generate Composite' : (expectedCount > 1 ? 'Generate 4 Images' : 'Generate Image');
      $('outputGrid').querySelectorAll('.skeleton-cell').forEach(sk => sk.remove());
      const got = d.images ? d.images.length : 0;
      if (got < expectedCount) {
        return { images: d.images || [], partial: true, expected: expectedCount, got, message: d.message || 'Partial: ' + got + '/' + expectedCount, session_id: d.session_id };
      }
      return { images: d.images || [], message: d.message || 'Done!', session_id: d.session_id };
    }
    if (d.status === 'error') {
      $('genLabel').textContent = state.prodImage ? 'Generate Composite' : (expectedCount > 1 ? 'Generate 4 Images' : 'Generate Image');
      return { error: d.error || 'Generation failed' };
    }
  }
}

$('clearGallery').addEventListener('click', () =\u003e {
  state.gallery = [];
  renderGallery();
});

async function refreshCost() {
  try {
    const r = await fetch('/api/costs', updateFetchOptions());
    const d = await r.json();
    $('costToday').textContent = '$' + (d.today?.toFixed(2) || '0.00');
  } catch (e) { console.log('cost fetch failed', e); }
}
refreshCost();

function showToast(msg, type) {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast ' + type;
  requestAnimationFrame(() => t.classList.add('show'));
  setTimeout(() => t.classList.remove('show'), 3000);
}

// ── Lightbox ──
const lightbox = {
  overlay: null, img: null, meta: null, list: [], idx: 0,
  init() {
    this.overlay = document.createElement('div');
    this.overlay.className = 'lightbox-overlay';
    this.overlay.innerHTML = (
      '<div class="lightbox-inner">' +
        '<button class="lightbox-close">×</button>' +
        '<img class="lightbox-img" src="" alt="">' +
        '<div class="lightbox-meta"></div>' +
        '<button class="lightbox-nav prev">‹</button>' +
        '<button class="lightbox-nav next">›</button>' +
      '</div>'
    );
    document.body.appendChild(this.overlay);
    this.img = this.overlay.querySelector('.lightbox-img');
    this.meta = this.overlay.querySelector('.lightbox-meta');
    this.overlay.querySelector('.lightbox-close').addEventListener('click', () => this.close());
    this.overlay.querySelector('.lightbox-nav.prev').addEventListener('click', (e) => { e.stopPropagation(); this.prev(); });
    this.overlay.querySelector('.lightbox-nav.next').addEventListener('click', (e) => { e.stopPropagation(); this.next(); });
    this.overlay.addEventListener('click', (e) => {
      if (e.target === this.overlay) this.close();
      const copyBtn = e.target.closest('.copy-lightbox');
      if (copyBtn) {
        const prompt = decodeURIComponent(copyBtn.dataset.prompt || '');
        if (prompt) {
          navigator.clipboard.writeText(prompt).then(() => showToast('Prompt copied', 'ok')).catch(() => showToast('Copy failed', 'err'));
        }
      }
    });
    document.addEventListener('keydown', (e) => {
      if (!this.overlay.classList.contains('active')) return;
      if (e.key === 'Escape') this.close();
      if (e.key === 'ArrowLeft') this.prev();
      if (e.key === 'ArrowRight') this.next();
    });
  },
  open(imgList, startIdx) {
    this.list = imgList;
    this.idx = startIdx || 0;
    this.render();
    this.overlay.classList.add('active');
    document.body.style.overflow = 'hidden';
  },
  close() {
    this.overlay.classList.remove('active');
    document.body.style.overflow = '';
  },
  render() {
    const img = this.list[this.idx];
    if (!img) return;
    this.img.src = img.url;
    const ratio = img.ratio || '';
    const cost = img.cost ? '$' + img.cost.toFixed(2) : '';
    const model = img.model ? img.model.replace('gemini-3.1-flash-image-preview', 'Flash').replace('gemini-3-pro-image-preview', 'Pro') : '';
    const prompt = img.prompt || state.lastPrompt || '';
    this.meta.innerHTML = (
      (ratio ? '<span class="pill ratio">' + ratio + '</span>' : '') +
      (cost ? '<span class="pill cost">' + cost + '</span>' : '') +
      (model ? '<span class="pill model">' + model + '</span>' : '') +
      (prompt ? '<span class="copy-lightbox" data-prompt="' + encodeURIComponent(prompt) + '" style="cursor:pointer;margin-left:6px;padding:4px 10px;border-radius:var(--radius-xs);background:rgba(255,255,255,0.08);color:#fff;font-size:0.72rem;"">📋 Copy prompt</span>' : '') +
      '<a href="' + img.url + '" download="' + img.name + '" style="margin-left:8px;padding:4px 10px;border-radius:var(--radius-xs);background:rgba(255,255,255,0.1);color:#fff;font-size:0.72rem;text-decoration:none;">Download</a>'
    );
  },
  prev() { if (this.idx > 0) { this.idx--; this.render(); } },
  next() { if (this.idx < this.list.length - 1) { this.idx++; this.render(); } }
};
lightbox.init();

// Wire lightbox clicks on output grid + gallery
function wireLightbox(container, getImgList) {
  container.addEventListener('click', (e) => {
    const img = e.target.closest('img');
    if (!img) return;
    const list = getImgList();
    const idx = list.findIndex(i => i.url === img.src);
    if (idx !== -1) lightbox.open(list, idx);
  });
}
wireLightbox($('outputGrid'), () => state.outputImages);
wireLightbox($('gallery'), () => state.gallery);

// ── Download all from output stage ──
$('downloadAllBtn').addEventListener('click', async () => {
  if (!state.outputImages.length) { showToast('No images to download', 'err'); return; }
  const urls = state.outputImages.map(img => img.url);
  try {
    const r = await fetch('/api/export-zip', updateFetchOptions({
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({urls})
    }));
    if (!r.ok) throw new Error('ZIP failed');
    const blob = await r.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'creative-studio-output.zip';
    a.click();
    showToast('ZIP downloaded', 'ok');
  } catch (e) { showToast('Export failed: ' + e.message, 'err'); }
});

// ── Keyboard shortcuts ──
$('prompt').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    $('genBtn').click();
  }
});

// ── Prompt history from server sessions ──
async function loadPromptHistory() {
  try {
    const r = await fetch('/api/sessions');
    const d = await r.json();
    const prompts = [];
    const seen = new Set();
    (d.sessions || []).forEach(s => {
      (s.entries || []).forEach(e => {
        const p = e.prompt || '';
        if (p && !seen.has(p)) { seen.add(p); prompts.push({ text: p, date: s.created_at || '' }); }
      });
    });
    const container = $('promptHistory');
    if (prompts.length) {
      $('promptHistoryPanel').style.display = 'block';
      container.innerHTML = prompts.slice(0, 10).map(p =>
        '<div class="prompt-history-item" data-prompt="' + encodeURIComponent(p.text) + '">' +
          '<span class="prompt-text">' + p.text + '</span>' +
          '<span class="prompt-meta">' + (p.date ? p.date.split('T')[0] : '') + '</span>' +
        '</div>'
      ).join('');
      container.querySelectorAll('.prompt-history-item').forEach(el => {
        el.addEventListener('click', () => {
          $('prompt').value = decodeURIComponent(el.dataset.prompt);
          $('prompt').focus();
        });
      });
    } else {
      $('promptHistoryPanel').style.display = 'none';
    }
  } catch (e) { console.log('prompt history load failed', e); }
}
loadPromptHistory();
</script>
</body>
</html>
"""



@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


# ── API Routes ──────────────────────────────────────────────────────────


@app.route("/api/generate", methods=["POST"])
@rate_limited
def api_generate():
    data = request.json or {}
    prompt = data.get("prompt", "").strip()
    if not prompt:
        return jsonify({"error": "Prompt required"}), 400

    mode = data.get("mode", "direct")
    tier = data.get("tier", "balanced")
    aspect = data.get("aspect_ratio", "16:9")
    smart = True
    variations = int(data.get("variations", 1))
    session_id = data.get("session_id", new_session_id())
    figma_url = data.get("figma_url")

    # Fetch figma context if requested
    figma_ctx = None
    if figma_url:
        file_key, node_id = parse_figma_url(figma_url)
        if file_key:
            figma_ctx = fetch_figma_context(file_key, node_id)
            if "error" not in figma_ctx:
                prompt = enhance_prompt_with_figma(prompt, figma_ctx)

    # Async: if variations > 1, run in background thread
    if variations > 1:
        job_id = _job_id()
        api_key = _get_api_key()
        if not api_key:
            return jsonify({"error": "No API key. Add your Gemini key in the sidebar panel or set GEMINI_API_KEY on the server."}), 400

        def _do_generate():
            images = []
            count = max(1, min(8, variations))
            for i in range(count):
                batch_images = run_cli_generate(prompt, mode, api_key, tier, aspect, smart, variations=1)
                if batch_images and "error" not in batch_images[0]:
                    img = batch_images[0]
                    add_entry(
                        session_id,
                        {
                            "type": mode,
                            "prompt": prompt[:100],
                            "cost": img.get("cost", 0),
                            "image_url": img.get("url", ""),
                            "model": img.get("model", ""),
                            "ratio": img.get("ratio", aspect),
                            "note": f"{img.get('name', '')} ({img.get('model', '')})",
                        },
                    )
                    images.append(img)
                    # Update job state incrementally so frontend can stream results
                    with _jobs_lock:
                        _jobs[job_id].setdefault("result", {})
                        _jobs[job_id]["result"]["images"] = images.copy()
                        _jobs[job_id]["result"]["progress"] = f"{i+1}/{count}"
                else:
                    # Stop on first failure
                    break
            costs = load_costs()
            costs["session_count"] = len(list(SESSIONS_DIR.glob("*.json")))
            save_costs(costs)
            return {"images": images, "session_id": session_id, "message": f"Generated {len(images)} image(s)"}

        _run_job_background(job_id, _do_generate)
        return jsonify({"job_id": job_id, "status": "running", "message": "Generation started"})

    # Sync: single image (fast path)
    api_key = _get_api_key()
    if not api_key:
        return jsonify({"error": "No API key. Add your Gemini key in the sidebar panel or set GEMINI_API_KEY on the server."}), 400

    images = run_cli_generate(prompt, mode, api_key, tier, aspect, smart, variations=1)
    for img in images:
        if "error" not in img:
            add_entry(
                session_id,
                {
                    "type": mode,
                    "prompt": prompt[:100],
                    "cost": img.get("cost", 0),
                    "image_url": img.get("url", ""),
                    "model": img.get("model", ""),
                    "ratio": img.get("ratio", aspect),
                    "note": f"{img.get('name', '')} ({img.get('model', '')})",
                },
            )

    costs = load_costs()
    costs["session_count"] = len(list(SESSIONS_DIR.glob("*.json")))
    save_costs(costs)

    return jsonify(
        {
            "message": f"Generated {len(images)} image(s)",
            "images": images,
            "session_id": session_id,
        }
    )


@app.route("/api/jobs/<job_id>", methods=["GET"])
@rate_limited
def api_job_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    resp = {
        "job_id": job_id,
        "status": job["status"],
        "started_at": job["started_at"],
    }
    if job["status"] == "done":
        resp.update(job["result"])
    elif job["status"] == "error":
        resp["error"] = job["error"]
    elif job["status"] == "running" and job.get("result"):
        # Stream partial results for batch generation
        resp["partial"] = job["result"]
    return jsonify(resp)


@app.route("/api/validate-key", methods=["POST"])
@rate_limited
def api_validate_key():
    """Check if a Gemini API key is valid by attempting a tiny generation."""
    data = request.json or {}
    key = data.get("key", "").strip()
    if not key:
        return jsonify({"error": "Key required"}), 400
    if not key.startswith("AIza"):
        return jsonify({"error": "Invalid format — Gemini keys start with AIza..."}), 400

    # Quick ping: attempt to list models via curl-like subprocess
    import urllib.request, urllib.error
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models?key={key}",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read()
            if resp.status == 200:
                return jsonify({"valid": True, "message": "Key is valid"})
    except urllib.error.HTTPError as e:
        if e.code == 400:
            return jsonify({"valid": False, "error": "Invalid API key"}), 200
        return jsonify({"valid": False, "error": f"HTTP {e.code}"}), 200
    except Exception as e:
        return jsonify({"valid": False, "error": str(e)}), 200

    return jsonify({"valid": True, "message": "Key looks valid"})


@app.route("/api/export-zip", methods=["POST"])
@rate_limited
def api_export_zip():
    import io, zipfile, urllib.request
    data = request.json or {}
    urls = data.get("urls", [])
    if not urls:
        return jsonify({"error": "No URLs provided"}), 400
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, url in enumerate(urls):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "CreativeStudio/1.0"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    ext = ".png"
                    ct = resp.headers.get("Content-Type", "")
                    if "jpeg" in ct or "jpg" in ct:
                        ext = ".jpg"
                    elif "webp" in ct:
                        ext = ".webp"
                    zf.writestr(f"image-{i+1}{ext}", resp.read())
            except Exception as e:
                zf.writestr(f"image-{i+1}-error.txt", str(e))
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name="creative-studio-export.zip",
    )


@app.route("/api/composite", methods=["POST"])
@rate_limited
def api_composite():
    if "product" not in request.files:
        return jsonify({"error": "Product image required"}), 400
    f = request.files["product"]
    prompt = request.form.get("prompt", "").strip()
    aspect = request.form.get("aspect_ratio", "16:9")
    session_id = request.form.get("session_id", new_session_id())
    if not prompt:
        return jsonify({"error": "Prompt required"}), 400

    tmp_dir = DATA_DIR / "uploads"
    tmp_dir.mkdir(exist_ok=True)
    product_path = tmp_dir / f"product_{int(time.time())}_{f.filename}"
    f.save(str(product_path))

    api_key = _get_api_key()
    if not api_key:
        return jsonify({"error": "No API key. Add your Gemini key in the sidebar panel or set GEMINI_API_KEY on the server."}), 400

    images = run_cli_composite(prompt, str(product_path), api_key, aspect)
    for img in images:
        add_entry(
            session_id,
            {
                "type": "composite",
                "prompt": prompt[:100],
                "cost": img.get("cost", 0),
                "image_url": img.get("url", ""),
                "model": img.get("model", ""),
                "ratio": img.get("ratio", aspect),
                "note": img.get("name", ""),
            },
        )

    return jsonify(
        {"message": "Composite generated", "images": images, "session_id": session_id}
    )


@app.route("/api/export", methods=["POST"])
@rate_limited
def api_export():
    session_id = request.form.get("session_id", new_session_id())
    presets = request.form.get("presets", "")
    if not presets:
        return jsonify({"error": "Presets required"}), 400

    # Case 1: Existing image from URL
    img_url = request.form.get("image_url")
    if img_url and img_url.startswith("/image/"):
        rel_path = img_url.replace("/image/", "")
        src_path = OUTPUT_DIR / rel_path
    # Case 2: Uploaded image
    elif "image" in request.files:
        f = request.files["image"]
        tmp_dir = DATA_DIR / "uploads"
        tmp_dir.mkdir(exist_ok=True)
        src_path = tmp_dir / f"export_{int(time.time())}_{f.filename}"
        f.save(str(src_path))
    else:
        return jsonify({"error": "Image required"}), 400

    images = run_cli_export(str(src_path), presets, _get_api_key())
    selected_list = [p.strip() for p in presets.split(",") if p.strip()]
    for img in images:
        add_entry(
            session_id,
            {
                "type": "export",
                "cost": 0,
                "image_url": img.get("url", ""),
                "model": "PIL",
                "note": f"Exported to:[{', '.join(selected_list)}]",
            },
        )

    return jsonify(
        {
            "message": f"Exported to {len(images)} formats",
            "images": images,
            "session_id": session_id,
        }
    )


@app.route("/api/export-track", methods=["POST"])
@rate_limited
def api_export_track():
    """Record which presets were used for a given exported image (metadata tracking)."""
    data = request.json or {}
    img_url = data.get("image_url", "")
    preset = data.get("preset", "")
    session_id = data.get("session_id") or None
    if session_id and img_url and preset:
        session_data = load_session(session_id)
        for e in reversed(session_data.get("entries", [])):
            if e.get("image_url") == img_url:
                # Append preset to existing note so we know which export formats were used
                existing_note = e.get("note", "")
                used_presets = []
                if existing_note:
                    m = re.search(r"Exported to:\[(.*?)\]", existing_note)
                    if m:
                        used_presets = [p.strip() for p in m.group(1).split(",")]
                if preset not in used_presets:
                    used_presets.append(preset)
                e["note"] = f"Exported to:[{', '.join(used_presets)}]"
                save_session(session_id, session_data)
    return jsonify({"ok": True})


@app.route("/api/qc", methods=["POST"])
@rate_limited
def api_qc():
    # Case 1: Existing image from URL (JSON or Form)
    data = request.json or request.form
    img_url = data.get("image_url")
    if img_url and img_url.startswith("/image/"):
        rel_path = img_url.replace("/image/", "")
        img_path = OUTPUT_DIR / rel_path
    # Case 2: Uploaded image
    elif "image" in request.files:
        f = request.files["image"]
        tmp_dir = DATA_DIR / "uploads"
        tmp_dir.mkdir(exist_ok=True)
        img_path = tmp_dir / f"qc_{int(time.time())}_{f.filename}"
        f.save(str(img_path))
    else:
        return jsonify({"error": "Image required"}), 400

    qc = run_cli_qc(str(img_path), _get_api_key())
    return jsonify({"message": f"QC Score: {qc['quality_score']}/10", "qc": qc})


@app.route("/api/figma", methods=["POST"])
@rate_limited
def api_figma():
    url = request.json.get("url")
    if not url:
        return jsonify({"error": "URL required"}), 400
    file_key, node_id = parse_figma_url(url)
    if not file_key:
        return jsonify({"error": "Invalid Figma URL"}), 400
    ctx = fetch_figma_context(file_key, node_id)
    return jsonify(ctx)


@app.route("/api/refine", methods=["POST"])
@rate_limited
def api_refine():
    data = request.json or {}
    image_path = data.get("image_path", "")
    changes = data.get("changes", "").strip()
    pins = data.get("pins", [])
    tier = data.get("tier", "quality")
    session_id = data.get("session_id", new_session_id())

    # Build spatial prompt from pins + user text
    pin_text = build_pin_prompt(pins) if pins else ""
    if pin_text and changes:
        full_changes = f"{changes}. Also: {pin_text}"
    elif pin_text:
        full_changes = pin_text
    elif changes:
        full_changes = changes
    else:
        return jsonify({"error": "changes or pins required"}), 400

    images = run_cli_refine(image_path, full_changes, _get_api_key(), tier)
    for img in images:
        add_entry(
            session_id,
            {
                "type": "refine",
                "cost": img.get("cost", 0),
                "image_url": img.get("url", ""),
                "model": img.get("model", ""),
                "note": full_changes[:200],
            },
        )

    return jsonify({"message": "Refined", "images": images, "session_id": session_id})


# ── Variations + Refine Routes ─────────────────────────────────────────────


@app.route("/api/variations", methods=["POST"])
@rate_limited
def api_variations():
    data = request.json or {}
    prompt = data.get("prompt", "").strip()
    if not prompt:
        return jsonify({"error": "Prompt required"}), 400

    count = int(data.get("count", 4))
    count = max(1, min(8, count))
    tier = data.get("tier", "balanced")
    aspect = data.get("aspect_ratio", "1:1")
    session_id = data.get("session_id", new_session_id())

    # Handle optional reference image upload
    input_image = None
    if "image" in request.files:
        f = request.files["image"]
        tmp_dir = DATA_DIR / "uploads"
        tmp_dir.mkdir(exist_ok=True)
        input_image = str(tmp_dir / f"variations_ref_{int(time.time())}_{f.filename}")
        f.save(input_image)

    images, session_key = run_cli_variations(_get_api_key(), 
        prompt=prompt,
        count=count,
        tier=tier,
        aspect=aspect,
        input_image=input_image,
    )

    for img in images:
        if "error" not in img:
            add_entry(
                session_id,
                {
                    "type": "variations",
                    "prompt": prompt[:100],
                    "cost": img.get("cost", 0),
                    "image_url": img.get("url", ""),
                    "model": img.get("model", ""),
                    "note": f"v{img.get('variation_index', '?')}",
                },
            )

    return jsonify(
        {
            "message": f"Generated {len(images)} variation(s)",
            "images": images,
            "session_key": session_key,
            "session_id": session_id,
        }
    )


@app.route("/api/variations/<session_key>/refine", methods=["POST"])
@rate_limited
def api_variations_refine(session_key):
    data = request.json or {}
    pick = int(data.get("pick", 1))  # 1-based variation index
    changes = data.get("changes", "").strip()
    tier = data.get("tier", "quality")
    session_id = data.get("session_id", new_session_id())

    if not changes:
        return jsonify({"error": "changes required"}), 400

    images = run_cli_refine_from_variation(
        session_key=session_key,
        pick_index=pick,
        changes=changes,
        tier=tier,
    )

    for img in images:
        if "error" not in img:
            add_entry(
                session_id,
                {
                    "type": "refine",
                    "prompt": f"Refine v{pick}: {changes[:80]}",
                    "cost": img.get("cost", 0),
                    "image_url": img.get("url", ""),
                    "model": img.get("model", ""),
                    "note": f"Refined from v{pick}",
                },
            )

    return jsonify(
        {"message": "Refined", "images": images, "session_id": session_id}
    )


# ── Chat Routes ─────────────────────────────────────────────────────────────


@app.route("/api/chat", methods=["POST"])
@rate_limited
def api_chat():
    data = request.json or {}
    prompt = data.get("prompt", "").strip()
    if not prompt:
        return jsonify({"error": "Prompt required"}), 400

    tier = data.get("tier", "balanced")
    aspect = data.get("aspect_ratio", "1:1")
    session_key = data.get("session_key", f"chat-{uuid.uuid4().hex[:8]}")
    session_id = data.get("session_id", new_session_id())

    # Optional starting image upload
    input_image = None
    if "image" in request.files:
        f = request.files["image"]
        tmp_dir = DATA_DIR / "uploads"
        tmp_dir.mkdir(exist_ok=True)
        input_image = str(tmp_dir / f"chat_ref_{int(time.time())}_{f.filename}")
        f.save(input_image)
        # If this is a fresh session, set the initial input
        if session_key not in _chat_sessions:
            pass  # run_cli_chat_turn handles first-turn initialization

    images, sess = run_cli_chat_turn(_get_api_key(), 
        session_key=session_key,
        prompt=prompt,
        tier=tier,
        aspect=aspect,
        input_image=input_image,
    )

    for img in images:
        if "error" not in img:
            add_entry(
                session_id,
                {
                    "type": "chat",
                    "prompt": prompt[:100],
                    "cost": img.get("cost", 0),
                    "image_url": img.get("url", ""),
                    "model": img.get("model", ""),
                    "note": f"Turn {img.get('turn', '?')}",
                },
            )

    return jsonify(
        {
            "message": "Turn complete",
            "images": images,
            "session_key": session_key,
            "session_id": session_id,
            "turn": sess.get("turn", 0),
        }
    )


@app.route("/api/chat/<session_key>/history", methods=["GET"])
@rate_limited
def api_chat_history(session_key):
    history = chat_session_history(session_key)
    sess = _chat_sessions.get(session_key, {})
    return jsonify(
        {
            "history": history,
            "turn": sess.get("turn", 0),
            "current_input": sess.get("current_input"),
        }
    )


@app.route("/api/chat/<session_key>/reset", methods=["POST"])
@rate_limited
def api_chat_reset(session_key):
    chat_reset(session_key)
    return jsonify({"message": "Chat session reset", "turn": 0})


@app.route("/api/chat/<session_key>/save", methods=["POST"])
@rate_limited
def api_chat_save(session_key):
    """Save the latest output from a chat session as a named file."""
    data = request.json or {}
    name = data.get("name", "").strip() or f"chat-{int(time.time())}"
    sess = _chat_sessions.get(session_key, {})
    current_input = sess.get("current_input")

    if not current_input or not Path(current_input).exists():
        return jsonify({"error": "No output to save"}), 400

    approved_dir = DATA_DIR / "approved"
    approved_dir.mkdir(exist_ok=True)
    dest = approved_dir / f"{name}.png"
    import shutil

    shutil.copy2(current_input, dest)
    return jsonify({"message": f"Saved as {name}", "path": str(dest), "url": image_url(str(dest))})


# ── Pin Annotation Routes ───────────────────────────────────────────────


@app.route("/api/pins", methods=["POST"])
@rate_limited
def api_pins_add():
    data = request.json or {}
    image_path = data.get("image_path", "")
    x = float(data.get("x", 0.5))
    y = float(data.get("y", 0.5))
    text = data.get("text", "").strip()
    if not image_path or not text:
        return jsonify({"error": "image_path and text required"}), 400
    pins = load_pins(image_path)
    pins.append({"id": pin_id(), "x": x, "y": y, "text": text, "time": now_str()})
    save_pins(image_path, pins)
    return jsonify({"pins": pins})


@app.route("/api/pins/<path:image_path>", methods=["GET"])
@rate_limited
def api_pins_get(image_path):
    if image_path and not image_path.startswith("/"):
        image_path = "/" + image_path
    return jsonify({"pins": load_pins(image_path)})


@app.route("/api/pins/<path:image_path>/<pin_id>", methods=["DELETE"])
@rate_limited
def api_pins_delete(image_path, pin_id):
    if image_path and not image_path.startswith("/"):
        image_path = "/" + image_path
    pins = [p for p in load_pins(image_path) if p.get("id") != pin_id]
    save_pins(image_path, pins)
    return jsonify({"pins": pins})


@app.route("/api/pins/<path:image_path>", methods=["DELETE"])
@rate_limited
def api_pins_clear(image_path):
    if image_path and not image_path.startswith("/"):
        image_path = "/" + image_path
    save_pins(image_path, [])
    return jsonify({"pins": []})


@app.route("/api/sessions", methods=["GET"])
@rate_limited
def api_sessions():
    sessions = []
    for p in sorted(
        SESSIONS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True
    ):
        data = load_json(p)
        sessions.append(
            {
                "id": data.get("id", p.stem),
                "created_at": data.get("created_at", ""),
                "entries": data.get("entries", []),
                "cost": sum(e.get("cost", 0) for e in data.get("entries", [])),
            }
        )
    return jsonify({"sessions": sessions})


@app.route("/api/session/<session_id>", methods=["GET"])
@rate_limited
def api_session_get(session_id):
    data = load_session(session_id)
    entries = []
    for e in data.get("entries", []):
        e2 = dict(e)
        e2["image_url"] = e.get("image_url", "")
        entries.append(e2)
    return jsonify(
        {"id": session_id, "entries": entries, "created_at": data.get("created_at", "")}
    )


@app.route("/api/costs", methods=["GET"])
@rate_limited
def api_costs():
    costs = load_costs()
    costs["session_count"] = len(list(SESSIONS_DIR.glob("*.json")))
    today = datetime.now().strftime("%Y-%m-%d")
    costs["today"] = costs.get("by_date", {}).get(today, 0.0)
    return jsonify(costs)


@app.route("/status")
def status_page():
    """Live status page showing container health, active jobs, cost today."""
    costs = load_costs()
    today = datetime.now().strftime("%Y-%m-%d")
    cost_today = costs.get("by_date", {}).get(today, 0.0)
    total = costs.get("total", 0.0)
    image_count = costs.get("image_count", 0)

    # Active jobs
    with _jobs_lock:
        jobs_list = []
        for jid, j in list(_jobs.items())[-20:]:
            jobs_list.append({
                "id": jid,
                "status": j["status"],
                "elapsed": round(time.time() - j["started_at"], 1) if j["started_at"] else None,
            })
        active_jobs = [j for j in jobs_list if j["status"] == "running"]

    # Build job rows HTML
    if active_jobs:
        job_rows = "\n".join(
            '<div class="job-row"><span>' + j["id"][:16] + '...</span><span class="badge running">running (' + str(j["elapsed"]) + 's)</span></div>'
            for j in active_jobs
        )
    else:
        job_rows = '<div class="job-row"><span>No active jobs</span><span class="badge ok">idle</span></div>'

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")
    cost_class = "warn" if cost_today > 3 else "ok"

    html = (
        '<!DOCTYPE html><html><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        '<title>Status | Creative Studio</title>'
        '<style>'
        ':root { --bg:#0a0a0f; --surface:#14141b; --border:rgba(255,255,255,0.08); --text:#f0f0f5; --text2:#9a9aa8; --ok:#2dd4a8; --warn:#fbbf24; --err:#f87171; --primary:#ff6b4a; --font:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif; }'
        'body { font-family:var(--font); background:var(--bg); color:var(--text); padding:40px 24px; max-width:640px; margin:0 auto; }'
        'h1 { font-size:1.3rem; margin-bottom:4px; } h1 span { color:var(--primary); }'
        '.subtitle { color:var(--text2); font-size:0.9rem; margin-bottom:28px; }'
        '.card { background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:20px; margin-bottom:16px; }'
        '.card-title { font-size:0.75rem; text-transform:uppercase; letter-spacing:0.04em; color:var(--text2); margin-bottom:12px; font-weight:600; }'
        '.metric { display:flex; justify-content:space-between; align-items:center; padding:8px 0; border-bottom:1px solid var(--border); }'
        '.metric:last-child { border:none; }'
        '.metric .val { font-weight:700; font-size:1.1rem; }'
        '.metric .lbl { color:var(--text2); font-size:0.85rem; }'
        '.ok { color:var(--ok); } .warn { color:var(--warn); } .err { color:var(--err); }'
        '.job-row { display:flex; justify-content:space-between; font-size:0.85rem; padding:6px 0; border-bottom:1px solid var(--border); }'
        '.job-row:last-child { border:none; }'
        '.badge { display:inline-block; padding:2px 8px; border-radius:100px; font-size:0.7rem; font-weight:600; }'
        '.badge.ok { background:rgba(45,212,168,0.12); color:var(--ok); }'
        '.badge.running { background:rgba(251,191,36,0.12); color:var(--warn); }'
        '.badge.err { background:rgba(248,113,113,0.12); color:var(--err); }'
        'a { color:var(--primary); text-decoration:none; } a:hover { text-decoration:underline; }'
        '.refresh { text-align:center; margin-top:20px; font-size:0.8rem; color:var(--text2); }'
        '</style></head><body>'
        '<h1>Creative Studio <span>Status</span></h1>'
        '<div class="subtitle">' + timestamp + '</div>'
        '<div class="card"><div class="card-title">Cost Tracker</div>'
        '<div class="metric"><span class="lbl">Today</span><span class="val ' + cost_class + '">$' + f"{cost_today:.2f}" + '</span></div>'
        '<div class="metric"><span class="lbl">All time</span><span class="val">$' + f"{total:.2f}" + '</span></div>'
        '<div class="metric"><span class="lbl">Images generated</span><span class="val">' + str(image_count) + '</span></div>'
        '</div>'
        '<div class="card"><div class="card-title">Active Jobs <span style="color:var(--text2);font-weight:400;">(' + str(len(active_jobs)) + ' running)</span></div>'
        + job_rows +
        '</div>'
        '<div class="card"><div class="card-title">Quick Links</div>'
        '<div class="metric"><span class="lbl"><a href="/">Back to Studio</a></span></span></div>'
        '<div class="metric"><span class="lbl"><a href="/api/costs">Raw costs JSON</a></span></span></div>'
        '</div>'
        '<div class="refresh">Auto-refreshes every 30s — or <a href="/status">reload now</a></div>'
        '<script>setTimeout(()=>location.reload(),30000);</script>'
        '</body></html>'
    )
    return html


@app.route("/docs")
def docs_page():
    """API documentation page."""
    html = (
        '<!DOCTYPE html><html><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        '<title>API Docs | Creative Studio</title>'
        '<style>'
        ':root { --bg:#0a0a0f; --surface:#14141b; --border:rgba(255,255,255,0.08); --text:#f0f0f5; --text2:#9a9aa8; --ok:#2dd4a8; --warn:#fbbf24; --err:#f87171; --primary:#ff6b4a; --font:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif; }'
        'body { font-family:var(--font); background:var(--bg); color:var(--text); padding:40px 24px; max-width:840px; margin:0 auto; }'
        'h1 { font-size:1.3rem; margin-bottom:4px; } h1 span { color:var(--primary); }'
        '.subtitle { color:var(--text2); font-size:0.9rem; margin-bottom:28px; }'
        '.card { background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:20px; margin-bottom:16px; }'
        '.card-title { font-size:0.75rem; text-transform:uppercase; letter-spacing:0.04em; color:var(--text2); margin-bottom:12px; font-weight:600; }'
        '.endpoint { margin-bottom:24px; }'
        '.endpoint:last-child { margin-bottom:0; }'
        '.method { display:inline-block; padding:2px 8px; border-radius:4px; font-size:0.72rem; font-weight:700; margin-right:8px; }'
        '.method.post { background:rgba(45,212,168,0.12); color:var(--ok); }'
        '.method.get { background:rgba(96,165,250,0.12); color:#60a5fa; }'
        '.path { font-family:monospace; font-size:0.9rem; color:var(--text); }'
        '.desc { color:var(--text2); font-size:0.85rem; margin:6px 0 10px; }'
        'pre { background:#0d0d12; border:1px solid var(--border); border-radius:8px; padding:12px; overflow-x:auto; font-size:0.78rem; color:#c4c4d0; margin:8px 0; }'
        'code { font-family:monospace; font-size:0.82rem; color:var(--primary); }'
        'table { width:100%; border-collapse:collapse; font-size:0.85rem; }'
        'th, td { text-align:left; padding:8px 12px; border-bottom:1px solid var(--border); }'
        'th { color:var(--text2); font-weight:600; font-size:0.75rem; text-transform:uppercase; letter-spacing:0.04em; }'
        'a { color:var(--primary); text-decoration:none; } a:hover { text-decoration:underline; }'
        '.nav { margin-bottom:24px; }'
        '.nav a { font-size:0.82rem; color:var(--text2); margin-right:16px; }'
        '</style></head><body>'
        '<div class="nav"><a href="/">← Studio</a> <a href="/status">Status</a> <a href="/history">History</a></div>'
        '<h1>Creative Studio <span>API Docs</span></h1>'
        '<div class="subtitle">Reference for the Creative Studio REST API. No auth required for reads. Writes need an API key.</div>'

        '<div class="card">'
        '<div class="card-title">Authentication</div>'
        '<p class="desc">Creative Studio runs in <strong>BYOK mode</strong>. Pass your Gemini API key via the <code>X-API-Key</code> header on every write request. We do not store your key. Costs are billed directly by Google.</p>'
        '<pre>curl -H "X-API-Key: YOUR_GEMINI_KEY" https://photogen.ashbi.ca/api/generate</pre>'
        '</div>'

        '<div class="card">'
        '<div class="card-title">Endpoints</div>'

        '<div class="endpoint">'
        '<span class="method get">GET</span><span class="path">/api/costs</span>'
        '<p class="desc">Get total spend, per-model breakdown, per-day breakdown, and image count.</p>'
        '<pre>curl https://photogen.ashbi.ca/api/costs</pre>'
        '</div>'

        '<div class="endpoint">'
        '<span class="method post">POST</span><span class="path">/api/validate-key</span>'
        '<p class="desc">Test whether a Gemini API key is valid before using it.</p>'
        '<pre>curl -X POST -H "X-API-Key: YOUR_KEY" https://photogen.ashbi.ca/api/validate-key</pre>'
        '</div>'

        '<div class="endpoint">'
        '<span class="method post">POST</span><span class="path">/api/generate</span>'
        '<p class="desc">Generate a single image. Returns immediately with a job ID. Poll <code>/api/jobs/&lt;id&gt;</code> for the result.</p>'
        '<pre>curl -X POST -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d \'{"model": "gemini-3.1-flash-image-preview", "prompt": "A sleek bottle on marble", "ratio": "1:1", "product_image": "base64..."}\' \
  https://photogen.ashbi.ca/api/generate</pre>'
        '</div>'

        '<div class="endpoint">'
        '<span class="method post">POST</span><span class="path">/api/composite</span>'
        '<p class="desc">Composite a product image onto a generated background. Same async pattern as /generate.</p>'
        '<pre>curl -X POST -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d \'{"prompt": "On a beach at sunset", "product_image": "base64...", "ratio": "4:3"}\' \
  https://photogen.ashbi.ca/api/composite</pre>'
        '</div>'

        '<div class="endpoint">'
        '<span class="method get">GET</span><span class="path">/api/jobs/&lt;job_id&gt;</span>'
        '<p class="desc">Poll for job status. Returns <code>running</code>, <code>done</code> (with image URL + cost), or <code>error</code>.</p>'
        '<pre>curl https://photogen.ashbi.ca/api/jobs/job_abc123</pre>'
        '</div>'

        '<div class="endpoint">'
        '<span class="method get">GET</span><span class="path">/image/&lt;path&gt;</span>'
        '<p class="desc">Serve a generated image. Paths are relative to the output directory.</p>'
        '</div>'

        '</div>'

        '<div class="card">'
        '<div class="card-title">Cost Table</div>'
        '<table>'
        '<tr><th>Model</th><th>Price / image</th></tr>'
        '<tr><td>gemini-3.1-flash-image-preview</td><td>$0.07</td></tr>'
        '<tr><td>gemini-3-pro-image-preview</td><td>$0.20</td></tr>'
        '<tr><td>imagen-4.0-fast-generate-001</td><td>$0.02</td></tr>'
        '<tr><td>imagen-4.0-generate-001</td><td>$0.04</td></tr>'
        '<tr><td>imagen-4.0-ultra-generate-001</td><td>$0.06</td></tr>'
        '</table>'
        '</div>'

        '</body></html>'
    )
    return html


@app.route("/history")
def history_page():
    """Show all past sessions from persistent data dir."""
    sessions = []
    if SESSIONS_DIR.exists():
        for sess_file in sorted(SESSIONS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                data = load_json(sess_file)
                created = datetime.fromtimestamp(sess_file.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                images = data.get("images", [])
                first_prompt = ""
                if images:
                    first_prompt = images[0].get("prompt", "")[:60] + ("..." if len(images[0].get("prompt", "")) > 60 else "")
                cost = sum(img.get("cost", 0) for img in images)
                model = images[0].get("model", "unknown") if images else "unknown"
                sessions.append({
                    "id": sess_file.stem,
                    "created": created,
                    "images": len(images),
                    "cost": cost,
                    "model": model,
                    "prompt": first_prompt,
                })
            except Exception:
                continue

    rows = []
    if sessions:
        for s in sessions[:100]:
            rows.append(
                '<tr>'
                '<td>' + s["created"] + '</td>'
                '<td><code>' + s["id"][:16] + '</code></td>'
                '<td>' + s["model"] + '</td>'
                '<td>' + str(s["images"]) + '</td>'
                '<td>$' + f"{s['cost']:.2f}" + '</td>'
                '<td style="max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="' + s["prompt"].replace('"', '&quot;') + '">' + s["prompt"] + '</td>'
                '</tr>'
            )
    else:
        rows = ['<tr><td colspan="6" style="text-align:center;color:var(--text2);padding:24px;">No sessions yet. Generate your first image in the Studio.</td></tr>']

    html = (
        '<!DOCTYPE html><html><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        '<title>History | Creative Studio</title>'
        '<style>'
        ':root { --bg:#0a0a0f; --surface:#14141b; --border:rgba(255,255,255,0.08); --text:#f0f0f5; --text2:#9a9aa8; --ok:#2dd4a8; --warn:#fbbf24; --err:#f87171; --primary:#ff6b4a; --font:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif; }'
        'body { font-family:var(--font); background:var(--bg); color:var(--text); padding:40px 24px; max-width:960px; margin:0 auto; }'
        'h1 { font-size:1.3rem; margin-bottom:4px; } h1 span { color:var(--primary); }'
        '.subtitle { color:var(--text2); font-size:0.9rem; margin-bottom:28px; }'
        '.card { background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:20px; margin-bottom:16px; overflow-x:auto; }'
        '.card-title { font-size:0.75rem; text-transform:uppercase; letter-spacing:0.04em; color:var(--text2); margin-bottom:12px; font-weight:600; }'
        'table { width:100%; border-collapse:collapse; font-size:0.85rem; }'
        'th, td { text-align:left; padding:10px 12px; border-bottom:1px solid var(--border); }'
        'th { color:var(--text2); font-weight:600; font-size:0.75rem; text-transform:uppercase; letter-spacing:0.04em; }'
        'tr:hover td { background:rgba(255,255,255,0.02); }'
        'code { font-family:monospace; font-size:0.8rem; background:#0d0d12; padding:2px 6px; border-radius:4px; color:var(--primary); }'
        'a { color:var(--primary); text-decoration:none; } a:hover { text-decoration:underline; }'
        '.nav { margin-bottom:24px; }'
        '.nav a { font-size:0.82rem; color:var(--text2); margin-right:16px; }'
        '.total { font-size:0.85rem; color:var(--text2); margin-top:12px; }'
        '</style></head><body>'
        '<div class="nav"><a href="/">← Studio</a> <a href="/status">Status</a> <a href="/docs">API Docs</a></div>'
        '<h1>Creative Studio <span>History</span></h1>'
        '<div class="subtitle">All past sessions sorted by newest first.</div>'
        '<div class="card">'
        '<div class="card-title">Sessions</div>'
        '<table>'
        '<tr><th>Date</th><th>Session ID</th><th>Model</th><th>Images</th><th>Cost</th><th>Prompt</th></tr>'
        + '\n'.join(rows) +
        '</table>'
        '</div>'
        '</body></html>'
    )
    return html


@app.route("/image/<path:subpath>")
@rate_limited
def serve_image(subpath):
    parts = subpath.split("/")
    if any(p in ("", ".", "..") or p.startswith("..") for p in parts):
        return jsonify({"error": "Invalid path"}), 400
    target = OUTPUT_DIR
    for part in parts:
        target = target / part
    # Prevent traversal outside OUTPUT_DIR
    try:
        resolved = target.resolve()
        base = OUTPUT_DIR.resolve()
        resolved.relative_to(base)
    except (ValueError, RuntimeError):
        return jsonify({"error": "Access denied"}), 403
    if resolved.exists() and resolved.is_file():
        return send_from_directory(str(resolved.parent), resolved.name)
    return jsonify({"error": "Not found"}), 404


# ─── Main ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5173)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    print(f"🎨 Creative Studio Web App running at http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)
