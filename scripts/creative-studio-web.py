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
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict
from functools import wraps

from flask import Flask, render_template_string, request, jsonify, send_from_directory

from figma_utils import parse_figma_url, fetch_figma_context, enhance_prompt_with_figma

# ─── Config ────────────────────────────────────────────────────────────
API_KEY = os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    raise RuntimeError("GEMINI_API_KEY environment variable is required")

# Session / cost / output dirs
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
    env["GEMINI_API_KEY"] = API_KEY
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
                }
            )

    if not images:
        return [{"error": "Generation produced no output"}]
    return images


def run_cli_composite(
    prompt: str, product_path: str, aspect: str, tier: str = "quality"
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
    env["GEMINI_API_KEY"] = API_KEY

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
                }
            ]
    except subprocess.CalledProcessError as e:
        return [{"error": f"Composite failed: {e.stderr[:500] if e.stderr else e}"}]
    except Exception as e:
        return [{"error": str(e)}]
    return [{"error": "Composite produced no output"}]


def run_cli_export(source_path: str, presets: str) -> List[Dict]:
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
    env["GEMINI_API_KEY"] = API_KEY
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


def run_cli_qc(image_path: str) -> dict:
    args = [
        "bash",
        str(Path(__file__).parent.parent / "launch.sh"),
        "qc",
        "--input",
        image_path,
    ]
    env = os.environ.copy()
    env["GEMINI_API_KEY"] = API_KEY
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


def run_cli_refine(image_path: str, changes: str, tier: str) -> List[Dict]:
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
    env["GEMINI_API_KEY"] = API_KEY
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
        env["GEMINI_API_KEY"] = API_KEY
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
    env["GEMINI_API_KEY"] = API_KEY
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
    env["GEMINI_API_KEY"] = API_KEY
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

# ── Frontend HTML ─────────────────────────────────────────────────────

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Creative Studio — AI Product Photography</title>
<style>
:root {
  --bg: #0a0a0f;
  --surface: #14141b;
  --surface-hover: #1c1c26;
  --border: rgba(255,255,255,0.08);
  --border-strong: rgba(255,255,255,0.14);
  --text: #f0f0f5;
  --text-secondary: #9a9aa8;
  --text-dim: #6a6a78;
  --primary: #ff6b4a;
  --primary-hover: #ff855a;
  --primary-glow: rgba(255,107,74,0.15);
  --radius:  12px;
  --radius-sm: 8px;
  --font: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; }
body {
  font-family: var(--font);
  background: var(--bg);
  color: var(--text);
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}
.app {
  max-width: 720px;
  margin: 0 auto;
  padding: 48px 24px 80px;
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  gap: 32px;
}
.brand { text-align: center; }
.brand h1 { font-size: 1.5rem; font-weight: 700; letter-spacing: -0.02em; }
.brand p { color: var(--text-secondary); font-size: 0.95rem; margin-top: 6px; }
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 28px;
  display: flex;
  flex-direction: column;
  gap: 20px;
}
.card-title {
  font-size: 0.85rem;
  font-weight: 600;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

/* ── Dropzone ── */
.dropzone {
  border: 2px dashed var(--border-strong);
  border-radius: var(--radius);
  padding: 40px 24px;
  text-align: center;
  cursor: pointer;
  transition: border-color 0.2s, background 0.2s;
  position: relative;
}
.dropzone:hover, .dropzone.dragover { border-color: var(--primary); background: var(--primary-glow); }
.dropzone input { position: absolute; inset: 0; opacity: 0; cursor: pointer; }
.dropzone .icon { font-size: 2rem; margin-bottom: 8px; }
.dropzone .label { font-weight: 600; font-size: 0.95rem; }
.dropzone .hint { color: var(--text-dim); font-size: 0.82rem; margin-top: 4px; }
.dropzone .file-name { margin-top: 10px; font-size: 0.85rem; color: var(--text-secondary); word-break: break-all; }
.preview-wrap {
  display: none;
  border-radius: var(--radius-sm);
  overflow: hidden;
  border: 1px solid var(--border);
  max-height: 260px;
}
.preview-wrap img { width: 100%; height: 100%; object-fit: cover; display: block; }

/* ── Prompt + Presets ── */
.prompt-area { display: flex; flex-direction: column; gap: 10px; }
.prompt-area label {
  font-size: 0.85rem; font-weight: 600; color: var(--text-secondary);
  text-transform: uppercase; letter-spacing: 0.04em;
}
.prompt-area textarea {
  width: 100%; padding: 14px; border: 1px solid var(--border);
  border-radius: var(--radius-sm); background: var(--bg); color: var(--text);
  font-family: var(--font); font-size: 0.95rem; line-height: 1.5;
  resize: vertical; min-height: 100px; outline: none; transition: border-color 0.2s;
}
.prompt-area textarea:focus { border-color: var(--primary); }
.prompt-area .hint { font-size: 0.8rem; color: var(--text-dim); }

/* ── Chips ── */
.chip-row { display: flex; gap: 10px; flex-wrap: wrap; }
.quality-chip, .aspect-chip, .preset-chip {
  padding: 8px 16px;
  border-radius: 100px;
  border: 1px solid var(--border);
  background: var(--bg);
  color: var(--text-secondary);
  font-size: 0.85rem;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s;
  user-select: none;
}
.quality-chip:hover, .aspect-chip:hover, .preset-chip:hover { border-color: var(--border-strong); color: var(--text); }
.quality-chip.active, .aspect-chip.active {
  border-color: var(--primary);
  background: var(--primary-glow);
  color: var(--primary);
}
.preset-chip { font-size: 0.78rem; padding: 6px 12px; }
.preset-chip.active {
  border-color: var(--primary);
  background: var(--primary-glow);
  color: var(--primary);
}

/* ── Remove button ── */
.remove-btn {
  padding: 8px 16px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  background: var(--surface-hover);
  color: var(--text-secondary);
  font-family: var(--font);
  font-size: 0.85rem;
  cursor: pointer;
  transition: all 0.15s;
  align-self: flex-start;
}
.remove-btn:hover { color: #f87171; border-color: rgba(248,113,113,0.3); }

/* ── Generate Button ── */
.gen-btn {
  padding: 16px 28px;
  border: none;
  border-radius: var(--radius);
  background: var(--primary);
  color: #fff;
  font-family: var(--font);
  font-size: 1rem;
  font-weight: 700;
  cursor: pointer;
  transition: background 0.2s, transform 0.15s;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
}
.gen-btn:hover { background: var(--primary-hover); transform: translateY(-1px); }
.gen-btn:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }
.gen-btn .spinner {
  width: 18px; height: 18px;
  border: 2px solid rgba(255,255,255,0.3);
  border-top-color: #fff;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
  display: none;
}
.gen-btn.generating .spinner { display: block; }
.gen-btn.generating .label { display: none; }
@keyframes spin { to { transform: rotate(360deg); } }

/* Mobile */
@media (max-width: 480px) {
  .output-grid { grid-template-columns: 1fr; }
  .output-cell img { height: 180px; }
  .app { padding: 24px 16px 60px; }
}

/* ── Output grid ── */
.output-wrap { display: none; flex-direction: column; gap: 16px; }
.output-wrap.show { display: flex; }
.output-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 12px;
}
.output-grid.single { grid-template-columns: 1fr; }
.output-cell {
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  overflow: hidden;
  background: var(--bg);
  position: relative;
}
.output-cell img { width: 100%; height: 220px; object-fit: cover; display: block; cursor: zoom-in; }
.output-cell .dl-overlay {
  position: absolute; bottom: 8px; right: 8px;
  background: rgba(0,0,0,0.6); color: #fff;
  padding: 6px 10px; border-radius: 6px; font-size: 0.75rem;
  text-decoration: none; opacity: 0; transition: opacity 0.2s;
}
.output-cell:hover .dl-overlay { opacity: 1; }
.output-meta {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 0.85rem;
  color: var(--text-secondary);
}

/* ── Gallery ── */
.gallery-card { display: none; }
.gallery-card.show { display: flex; }
.gallery {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(90px, 1fr));
  gap: 8px;
}
.gallery-thumb {
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  overflow: hidden;
  cursor: pointer;
  opacity: 0.7;
  transition: opacity 0.15s, border-color 0.15s;
}
.gallery-thumb:hover, .gallery-thumb.active { opacity: 1; border-color: var(--primary); }
.gallery-thumb { position: relative; }
.gallery-thumb .del {
  position: absolute; top: 2px; right: 2px;
  background: rgba(0,0,0,0.5); color: #fff;
  width: 20px; height: 20px; border-radius: 50%;
  font-size: 12px; line-height: 20px; text-align: center;
  cursor: pointer; opacity: 0; transition: opacity 0.15s;
}
.gallery-thumb:hover .del { opacity: 1; }
.gallery-thumb img { width: 100%; height: 90px; object-fit: cover; display: block; }

/* ── Toast / Cost ── */
.toast {
  position: fixed;
  bottom: 24px;
  left: 50%;
  transform: translateX(-50%) translateY(20px);
  padding: 12px 24px;
  border-radius: var(--radius);
  font-size: 0.9rem;
  font-weight: 500;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.3s, transform 0.3s;
  z-index: 100;
}
.toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
.toast.ok { background: #1a3a2f; color: #2dd4a8; border: 1px solid rgba(45,212,168,0.2); }
.toast.err { background: #3a1a1a; color: #f87171; border: 1px solid rgba(248,113,113,0.2); }

.cost-pill {
  text-align: center;
  font-size: 0.8rem;
  color: var(--text-dim);
}
.cost-pill input {
  width: 55px;
  background: transparent;
  color: var(--text);
  border: none;
  border-bottom: 1px solid var(--border);
  text-align: center;
  font-size: 0.8rem;
  font-family: var(--font);
  padding: 2px 4px;
}
</style>
</head>
<body>
<div class="app">
  <div class="brand">
    <h1>Creative Studio</h1>
    <p>AI product photography for CPG &amp; DTC brands</p>
  </div>

  <!-- 1. Product Upload -->
  <div class="card">
    <div class="card-title">1. Your Product</div>
    <div class="dropzone" id="dropzone">
      <input type="file" id="fileInput" accept="image/*">
      <div class="icon">&#128247;</div>
      <div class="label">Click or drop your product photo</div>
      <div class="hint">PNG / JPG / WEBP — helps the AI keep your exact packaging</div>
      <div class="file-name" id="fileName"></div>
    </div>
    <div class="preview-wrap" id="previewWrap">
      <img id="previewImg" alt="Product preview">
    </div>
    <button class="remove-btn" id="removeBtn" style="display:none;">Remove product</button>
  </div>

  <!-- 2. Scene + Presets -->
  <div class="card">
    <div class="card-title">2. Scene</div>
    <div class="prompt-area">
      <label for="prompt">Describe the shot</label>
      <textarea id="prompt" placeholder="e.g. Premium protein tub on a clean oak shelf in a boutique fitness store, warm overhead lighting, shallow depth of field, product photography style"></textarea>
      <div class="chip-row" id="presetRow">
        <div class="preset-chip" data-preset="amazon">Amazon white</div>
        <div class="preset-chip" data-preset="instagram">Instagram lifestyle</div>
        <div  class="preset-chip" data-preset="email">Email banner</div>
        <div class="preset-chip" data-preset="pinterest">Pinterest</div>
      </div>
      <div class="hint">Be specific about setting, lighting, and mood. The AI builds the scene around your product.</div>
    </div>
  </div>

  <!-- 3. Aspect Ratio -->
  <div class="card">
    <div class="card-title">3. Aspect Ratio</div>
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
  <div class="card">
    <div class="card-title">4. Quality</div>
    <div class="chip-row" id="qualityRow">
      <div class="quality-chip active" data-tier="fast" data-cost="0.07">Fast &middot; $0.07 &middot; draft</div>
      <div class="quality-chip" data-tier="balanced" data-cost="0.07">Balanced &middot; $0.07 &middot; 2K</div>
      <div class="quality-chip" data-tier="quality" data-cost="0.20">Quality &middot; $0.20 &middot; 2K</div>
    </div>
  </div>

  <!-- Batch toggle -->
  <div class="batch-row" id="batchRow" style="display:flex; gap:10px; align-items:center; justify-content:center; font-size:0.85rem; color:var(--text-secondary);">
    <label style="cursor:pointer; display:flex; align-items:center; gap:6px;">
      <input type="checkbox" id="batchToggle" style="accent-color:var(--primary);">
      Generate 4 variations (slower)
    </label>
  </div>

  <!-- 5. Generate -->
  <button class="gen-btn" id="genBtn">
    <div class="spinner"></div>
    <span class="label" id="genLabel">Generate 4 Images</span>
  </button>

  <!-- Output -->
  <div class="output-wrap" id="outputWrap">
    <div class="output-grid" id="outputGrid"></div>
    <div class="output-meta">
      <span id="outputMeta">Generated &middot; 0 images</span>
      <a class="download-btn" id="downloadBtn" download>Download All</a>
    </div>
  </div>

  <!-- Session Gallery -->
  <div class="card gallery-card" id="galleryCard">
    <div class="card-title">Session Gallery</div>
    <div class="gallery" id="gallery"></div>
    <button class="gen-btn" id="clearGallery" style="margin-top:12px; padding:8px 16px; font-size:0.85rem; background:var(--surface-hover); color:var(--text-secondary);">Clear Gallery</button>
  </div>

  <div class="cost-pill">
    Cost today: <span id="costToday">$0.00</span> &nbsp;|&nbsp;
    Limit: $<input type="number" id="costLimit" value="5.00" step="0.50" min="0">
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const $ = id => document.getElementById(id);
let state = { tier: 'fast', aspect: '1:1', prodImage: null, generating: false, gallery: [] };

const PRESETS = {
  amazon:    { prompt: 'Clean pure white background, soft shadow underneath, studio lighting, product centered, ecommerce photography, high detail', aspect: '1:1' },
  instagram: { prompt: 'Lifestyle flatlay on textured surface, natural soft window light from left, shallow depth of field, lifestyle product photography', aspect: '1:1' },
  email:     { prompt: 'Product on clean gradient background, dramatic side lighting, hero shot, wide composition', aspect: '16:9' },
  pinterest: { prompt: 'Product in styled scene with complementary props, warm golden tones, overhead 45 degree angle, editorial style', aspect: '4:5' },
};

// ── Chip selectors ──
function initChips(rowId, key, cls) {
  $(rowId).addEventListener('click', e => {
    const chip = e.target.closest('.' + cls);
    if (!chip) return;
    document.querySelectorAll('#' + rowId + ' .' + cls).forEach(c => c.classList.remove('active'));
    chip.classList.add('active');
    state[key] = chip.dataset.tier || chip.dataset.ratio || chip.dataset.preset;
  });
}
initChips('qualityRow', 'tier', 'quality-chip');

// Aspect chips
$('aspectRow').addEventListener('click', e => {
  const chip = e.target.closest('.aspect-chip');
  if (!chip) return;
  document.querySelectorAll('#aspectRow .aspect-chip').forEach(c => c.classList.remove('active'));
  chip.classList.add('active');
  state.aspect = chip.dataset.ratio;
});

// ── Presets ──
$('presetRow').addEventListener('click', e => {
  const chip = e.target.closest('.preset-chip');
  if (!chip) return;
  const key = chip.dataset.preset;
  const p = PRESETS[key];
  if (!p) return;
  $('prompt').value = p.prompt;
  state.aspect = p.aspect;
  // highlight matching aspect chip
  document.querySelectorAll('.aspect-chip').forEach(c => {
    c.classList.toggle('active', c.dataset.ratio === p.aspect);
  });
  // highlight preset
  document.querySelectorAll('.preset-chip').forEach(c => c.classList.remove('active'));
  chip.classList.add('active');
});

// ── File dropzone ──
const dz = $('dropzone'), fi = $('fileInput');
const onFile = file => {
  if (!file || !file.type.startsWith('image/')) return;
  state.prodImage = file;
  $('fileName').textContent = file.name;
  const url = URL.createObjectURL(file);
  $('previewImg').src = url;
  $('previewWrap').style.display = 'block';
  $('removeBtn').style.display = 'block';
  updateGenLabel();
};
fi.addEventListener('change', e => onFile(e.target.files[0]));

$('removeBtn').addEventListener('click', () => {
  state.prodImage = null;
  $('fileName').textContent = '';
  $('previewWrap').style.display = 'none';
  $('removeBtn').style.display = 'none';
  fi.value = '';
  updateGenLabel();
});
dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('dragover'); });
dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
dz.addEventListener('drop', e => {
  e.preventDefault(); dz.classList.remove('dragover');
  onFile(e.dataTransfer.files[0]);
});

function updateGenLabel() {
  const batch = $('batchToggle').checked;
  const count = state.prodImage ? 1 : (batch ? 4 : 1);
  const label = state.prodImage ? 'Generate Composite' : (batch ? 'Generate 4 Images' : 'Generate Image');
  $('genLabel').textContent = label;
}
$('batchToggle').addEventListener('change', updateGenLabel);

async function getCostToday() {
  try {
    const r = await fetch('/api/costs');
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
    thumb.className = 'gallery-thumb';
    thumb.innerHTML = `<img src="${img.url}" alt=""><div class="del" data-idx="${idx}">×</div>`;
    thumb.querySelector('.del').addEventListener('click', (e) => {
      e.stopPropagation();
      state.gallery.splice(idx, 1);
      renderGallery();
    });
    thumb.addEventListener('click', () => loadIntoOutput([img]));
    g.appendChild(thumb);
  });
  $('galleryCard').classList.toggle('show', state.gallery.length > 0);
}

function loadIntoOutput(images) {
  const grid = $('outputGrid');
  grid.innerHTML = '';
  grid.className = 'output-grid' + (images.length === 1 ? ' single' : '');
  images.forEach(img => {
    const cell = document.createElement('div');
    cell.className = 'output-cell';
    cell.innerHTML = `<img src="${img.url}" alt=""><a class="dl-overlay" href="${img.url}" download="${img.name}">Download</a>`;
    grid.appendChild(cell);
  });
  const totalCost = images.reduce((s, img) => s + (img.cost || 0), 0);
  $('outputMeta').textContent = `Generated · ${images.length} image${images.length > 1 ? 's' : ''} · $${totalCost.toFixed(2)}`;
  $('clearGallery').style.display = images.length > 1 ? 'none' : 'block';
  // Update main download btn to zip all (fallback: first image)
  const first = images[0];
  $('downloadBtn').href = first ? first.url : '#';
  $('downloadBtn').download = first ? first.name : '';
  $('outputWrap').classList.add('show');
}

// ── Generate ──
$('genBtn').addEventListener('click', async () => {
  const prompt = $('prompt').value.trim();
  if (!prompt) { showToast('Enter a scene description', 'err'); return; }
  if (state.generating) return;

  // Cost guardrail
  const limit = parseFloat($('costLimit').value) || 5;
  if (limit < 0 || isNaN(limit)) { showToast('Invalid cost limit', 'err'); return; }
  const costToday = await getCostToday();
  const batch = $('batchToggle').checked;
  const count = state.prodImage ? 1 : (batch ? 4 : 1);
  const est = state.prodImage ? 0.20 : (0.07 * count);
  if (costToday + est > limit) {
    showToast(`Would exceed $${limit.toFixed(2)} cost limit. Raise limit or wait until tomorrow.`, 'err');
    return;
  }

  state.generating = true;
  $('genBtn').disabled = true;
  $('genBtn').classList.add('generating');
  $('outputWrap').classList.remove('show');

  try {
    let data;
    if (state.prodImage) {
      const fd = new FormData();
      fd.append('prompt', prompt);
      fd.append('product', state.prodImage);
      fd.append('aspect_ratio', state.aspect);
      fd.append('tier', state.tier);
      const resp = await fetch('/api/composite', { method: 'POST', body: fd });
      data = await resp.json();
    } else {
      const batch = $('batchToggle').checked;
      const count = batch ? 4 : 1;
      const body = {
        prompt, mode: 'direct', tier: state.tier,
        aspect_ratio: state.aspect, variations: count
      };
      const resp = await fetch('/api/generate', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      data = await resp.json();
    }

    if (data.error || (data.images && data.images[0]?.error)) {
      showToast(data.error || data.images[0].error || 'Generation failed', 'err');
      return;
    }

    if (data.images && data.images.length) {
      loadIntoOutput(data.images);
      addToGallery(data.images);
      showToast(data.message || 'Done!', 'ok');
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

$('clearGallery').addEventListener('click', () => {
  state.gallery = [];
  renderGallery();
});

async function refreshCost() {
  try {
    const r = await fetch('/api/costs');
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
    # Force smart enhancement on every request — improves output quality regardless of user input
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

    images = run_cli_generate(prompt, mode, tier, aspect, smart, variations=variations)
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

    images = run_cli_composite(prompt, str(product_path), aspect)
    for img in images:
        add_entry(
            session_id,
            {
                "type": "composite",
                "prompt": prompt[:100],
                "cost": img.get("cost", 0),
                "image_url": img.get("url", ""),
                "model": img.get("model", ""),
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

    images = run_cli_export(str(src_path), presets)
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

    qc = run_cli_qc(str(img_path))
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

    images = run_cli_refine(image_path, full_changes, tier)
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

    images, session_key = run_cli_variations(
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

    images, sess = run_cli_chat_turn(
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
