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

from flask import Flask, render_template, request, jsonify, send_from_directory, send_file

from figma_utils import parse_figma_url, fetch_figma_context, enhance_prompt_with_figma


# Single source of truth for the app version. Read this in /api/whoami and
# any other code that needs the public version. Update it as part of release
# and update pyproject.toml to match. The previous build had three different
# version numbers across web.py / pyproject.toml / README.md.
__version__ = "4.6.0"

# Max bytes for a user-supplied prompt. Defense against a 100KB figma-context
# concat blowing up the subprocess argv (POSIX ARG_MAX is ~128KB on Linux;
# macOS is 256KB, but a runaway length will OOM the worker or break the
# shell). 16KB is enough for a full figma palette + a 2KB user brief.
_MAX_PROMPT_BYTES = int(os.environ.get("CREATIVE_MAX_PROMPT_BYTES", str(16 * 1024)))


def _enforce_prompt_length(prompt: str):
    """Reject a prompt that exceeds _MAX_PROMPT_BYTES. Returns either None
    (allowed) or a (jsonify_response, 413) tuple for the caller to return.
    Counts bytes (not chars) so a Unicode paste doesn't sneak past with a
    huge BMP-replacement-char payload.
    """
    if not prompt:
        return None
    if len(prompt.encode("utf-8")) > _MAX_PROMPT_BYTES:
        return (
            jsonify({
                "error": f"Prompt too long: {len(prompt.encode('utf-8'))} bytes > limit {_MAX_PROMPT_BYTES} bytes. Shorten your prompt or the figma context.",
                "limit": _MAX_PROMPT_BYTES,
            }),
            413,
        )
    return None

# ─── Config ────────────────────────────────────────────────────────────
# ── BYOK / server-fallback config ───────────────────────────────────────
# GEMINI_API_KEY env var sets a server-side fallback key (opt-in).
# CREATIVE_ALLOW_SERVER_FALLBACK=true is REQUIRED for the fallback to be used.
# When the fallback is disabled, every generation endpoint requires a user-supplied
# key via the X-API-Key header. Default OFF for shipped/public deploys.
SERVER_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
ALLOW_SERVER_FALLBACK = os.environ.get("CREATIVE_ALLOW_SERVER_FALLBACK", "").lower() in ("1", "true", "yes")


def _get_api_key() -> str:
    """Return the active API key for this request.

    Order:
    1. X-API-Key header (per-request BYOK)
    2. Server fallback (only if CREATIVE_ALLOW_SERVER_FALLBACK=true)
    3. Empty string (caller should reject with 402)
    """
    user_key = request.headers.get("X-API-Key", "").strip()
    if user_key:
        return user_key
    if ALLOW_SERVER_FALLBACK and SERVER_API_KEY:
        return SERVER_API_KEY
    return ""


def _require_api_key() -> tuple:
    """Return (key, None) if a key is available, else (None, error_response).

    Returns a tuple of (Optional[str], Optional[Response]) so callers can do
    `api_key, err = _require_api_key(); if err: return err`.
    """
    key: Optional[str] = _get_api_key()
    if not key:
        return None, (
            jsonify(
                {
                    "error": "BYOK required",
                    "message": (
                        "Add your Gemini API key in the editor sidebar. "
                        "We don't store or train on your key — cost is billed directly to your Google account."
                    ),
                }
            ),
            402,
        )
    return key, None


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

# All JSON read-mutate-write paths (load_costs, save_costs, save_pins,
# add_entry, save_session) serialize on this lock. Without it, two
# concurrent track_cost() calls each read costs, each +0.05, one writes
# and the other's increment is lost.
_json_lock = threading.Lock()


def _with_json_lock(fn):
    """Run a read-mutate-write JSON path under a single lock."""
    with _json_lock:
        return fn()


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
# Per-image cost by model and resolution (matches CLI PRICE_CARD)
COSTS = {
    "gemini-3.1-flash-image-preview": {"1K": 0.045, "2K": 0.090, "4K": 0.180},
    "gemini-3-pro-image-preview":     {"1K": 0.134, "2K": 0.240, "4K": 0.480},
    "imagen-4.0-fast-generate-001":   {"1K": 0.02},
    "imagen-4.0-generate-001":        {"1K": 0.04, "2K": 0.04, "4K": 0.06},
    "imagen-4.0-ultra-generate-001":  {"1K": 0.06},
}

# Tier → (model, resolution) — matches CLI _TIER_MAP
_TIER_MODEL = {
    "fast":     ("imagen-4.0-fast-generate-001",   "1K"),
    "balanced": ("gemini-3.1-flash-image-preview", "1K"),
    "quality":  ("gemini-3.1-flash-image-preview", "2K"),
    "ultra":    ("gemini-3-pro-image-preview",     "2K"),
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


def track_cost(model: str, resolution: str = "1K", count: int = 1):
    """Charge the user for `count` images at the given model/resolution tier.

    Atomic read-mutate-write under _json_lock so concurrent calls don't lose
    increments. The cost guardrail uses the same lock via _try_charge_costs()
    so the limit is enforced exactly once per request.
    """
    def _do():
        costs = load_costs()
        price_map = COSTS.get(model, {})
        if isinstance(price_map, dict):
            unit = price_map.get(resolution) or price_map.get("1K") or 0.04
        else:
            unit = float(price_map) if price_map else 0.04
        c = unit * count
        costs["total"] += c
        costs["by_model"][model] = costs["by_model"].get(model, 0.0) + c
        today = datetime.now().strftime("%Y-%m-%d")
        costs["by_date"][today] = costs["by_date"].get(today, 0.0) + c
        costs["image_count"] += count
        save_costs(costs)
        return c
    return _with_json_lock(_do)


def _check_daily_limit(est_count: int = 1, tier: str = "balanced"):
    """Atomically check whether `est_count` images at `tier` would push today's
    spend past the CREATIVE_DAILY_LIMIT cap.

    Returns:
        None if the request is allowed to proceed.
        A (response, status) tuple to short-circuit with 429.

    The check is held under _json_lock so two concurrent callers cannot
    both pass the limit and both proceed.
    """
    def _do():
        try:
            daily_limit = float(os.environ.get("CREATIVE_DAILY_LIMIT", "5"))
        except (TypeError, ValueError):
            daily_limit = 5.0
        if daily_limit <= 0:
            return None
        unit = cost_for_tier(tier)
        est_total = unit * max(1, est_count)
        costs_now = load_costs()
        today = datetime.now().strftime("%Y-%m-%d")
        spent_today = float(costs_now.get("by_date", {}).get(today, 0.0))
        if spent_today + est_total > daily_limit:
            return (
                jsonify({
                    "error": f"Daily limit ${daily_limit:.2f} reached. Spent today: ${spent_today:.2f}. Request would cost ${est_total:.2f}. Set CREATIVE_DAILY_LIMIT to a higher value or wait until tomorrow.",
                    "spent_today": round(spent_today, 4),
                    "limit": daily_limit,
                    "est_cost": round(est_total, 4),
                }),
                429,
            )
        return None
    return _with_json_lock(_do)


# Backward-compat alias. Old callers using enforce_daily_limit() as a pure
# check (no charge) still work — it now serializes on the same lock as the
# post-generation track_cost() so the limit can't be bypassed by racing
# requests. New code should prefer _check_daily_limit.
def enforce_daily_limit(est_count: int = 1, tier: str = "balanced"):
    return _check_daily_limit(est_count, tier)


def cost_for_tier(tier: str) -> float:
    """Estimated per-image cost for a quality tier."""
    model, res = _TIER_MODEL.get(tier, ("gemini-3.1-flash-image-preview", "1K"))
    price_map = COSTS.get(model, {})
    if isinstance(price_map, dict):
        return price_map.get(res, price_map.get("1K", 0.04))
    return float(price_map) if price_map else 0.04


def enforce_daily_limit(est_count: int = 1, tier: str = "balanced"):
    """Check the CREATIVE_DAILY_LIMIT and reject the request with 429 if it would push today's spend past the cap.

    Returns (None, None) if allowed, or (response, status) tuple to short-circuit.
    """
    try:
        daily_limit = float(os.environ.get("CREATIVE_DAILY_LIMIT", "5"))
    except (TypeError, ValueError):
        daily_limit = 5.0
    if daily_limit <= 0:
        return None
    unit = cost_for_tier(tier)
    est_total = unit * max(1, est_count)
    costs_now = load_costs()
    today = datetime.now().strftime("%Y-%m-%d")
    spent_today = float(costs_now.get("by_date", {}).get(today, 0.0))
    if spent_today + est_total > daily_limit:
        return (
            jsonify({
                "error": f"Daily limit ${daily_limit:.2f} reached. Spent today: ${spent_today:.2f}. Request would cost ${est_total:.2f}. Set CREATIVE_DAILY_LIMIT to a higher value or wait until tomorrow.",
                "spent_today": round(spent_today, 4),
                "limit": daily_limit,
                "est_cost": round(est_total, 4),
            }),
            429,
        )
    return None


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
    def _do():
        data = load_session(session_id)
        data["entries"].append({"time": now_str(), **entry})
        save_session(session_id, data)
    _with_json_lock(_do)


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


def _safe_output_relpath(rel_path: str) -> Optional[Path]:
    """Resolve a user-supplied /image/<rel> tail against OUTPUT_DIR, rejecting
    any path that escapes it (the same guard serve_image() already uses).
    Returns the resolved Path if safe, None if traversal or non-existent.
    """
    if not rel_path:
        return None
    # Reject obvious traversal tokens at every path component
    parts = rel_path.split("/")
    if any(p in ("", ".", "..") or p.startswith("..") for p in parts):
        return None
    target = OUTPUT_DIR
    for part in parts:
        target = target / part
    try:
        resolved = target.resolve()
        base = OUTPUT_DIR.resolve()
        resolved.relative_to(base)
    except (ValueError, RuntimeError):
        return None
    if not resolved.exists() or not resolved.is_file():
        return None
    return resolved


# ─── Pin Annotations ────────────────────────────────────────────────────
PINS_DB = DATA_DIR / "pins.json"


def load_pins(image_path: str) -> List[Dict]:
    data = load_json(PINS_DB, {})
    return data.get(image_path, [])


def save_pins(image_path: str, pins: List[Dict]):
    def _do():
        data = load_json(PINS_DB, {})
        data[image_path] = pins
        save_json(PINS_DB, data)
    _with_json_lock(_do)


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

# NOTE: _TIER_MODEL is defined earlier in the file (line ~89) as a (model, resolution) tuple map.
# Don't redefine it here — old duplicates caused a critical bug where the second definition
# shadowed the first and broke the daily-limit guardrail.


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
    # Map tier → resolution so the CLI charges the correct amount
    _tier_info = _TIER_MODEL.get(tier, ("gemini-3.1-flash-image-preview", "1K"))
    resolution = _tier_info[1] if isinstance(_tier_info, tuple) else "1K"
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
            "--resolution",
            resolution,
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
            model_used, resolution = _TIER_MODEL.get(tier, ("gemini-3-pro-image-preview", "2K"))
            cost = track_cost(model_used, resolution)
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

        _tier_info = _TIER_MODEL.get(tier, ("gemini-3.1-flash-image-preview", "1K"))
        _resolution = _tier_info[1] if isinstance(_tier_info, tuple) else "1K"
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
            "--resolution",
            _resolution,
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
                model_used, resolution = _TIER_MODEL.get(tier, ("gemini-3-pro-image-preview", "2K"))
                cost = track_cost(model_used, resolution)
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
        "model": _TIER_MODEL.get(tier, ("gemini-3-pro-image-preview", "2K"))[0],
        "resolution": _TIER_MODEL.get(tier, ("gemini-3-pro-image-preview", "2K"))[1],
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

    _tier_info = _TIER_MODEL.get(tier, ("gemini-3.1-flash-image-preview", "1K"))
    _resolution = _tier_info[1] if isinstance(_tier_info, tuple) else "1K"
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
        "--resolution",
        _resolution,
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
            model_used, resolution = _TIER_MODEL.get(tier, ("gemini-3-pro-image-preview", "2K"))
            cost = track_cost(model_used, resolution)
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

    _tier_info = _TIER_MODEL.get(tier, ("gemini-3.1-flash-image-preview", "1K"))
    _resolution = _tier_info[1] if isinstance(_tier_info, tuple) else "1K"
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
        "--resolution",
        _resolution,
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
            model_used, resolution = _TIER_MODEL.get(tier, ("gemini-3-pro-image-preview", "2K"))
            cost = track_cost(model_used, resolution)
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
APP_ROOT = Path(__file__).parent.parent
TEMPLATES_DIR = APP_ROOT / "templates"
STATIC_DIR = APP_ROOT / "static"
app = Flask(__name__, template_folder=str(TEMPLATES_DIR), static_folder=str(STATIC_DIR))
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(32))
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32MB uploads

# ── Error logging ──
# Persist warnings+ to a rotating log so failures are debuggable via SSH
# when the app breaks. No external Sentry needed for a side project.
import logging
from logging.handlers import RotatingFileHandler
if not app.debug:
    try:
        _log_dir = DATA_DIR if DATA_DIR else Path("/tmp")
        _log_dir.mkdir(parents=True, exist_ok=True)
        _err_log = _log_dir / "flask-errors.log"
        _handler = RotatingFileHandler(
            str(_err_log), maxBytes=10_000_000, backupCount=3, encoding="utf-8"
        )
        _handler.setLevel(logging.WARNING)
        _handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s "
            "[in %(pathname)s:%(lineno)d]"
        ))
        app.logger.addHandler(_handler)
        app.logger.setLevel(logging.WARNING)
        app.logger.info("Creative Studio starting (logger attached: %s)", _err_log)
    except Exception as _e:
        # If we can't attach a file handler, fall back to stderr only —
        # don't break the app boot over a logger setup failure
        logging.basicConfig(level=logging.WARNING)
        app.logger.warning("Failed to attach rotating file handler: %s", _e)

# ── Simple in-memory rate limiter ───────────────────────────────────────
_request_log: Dict[str, list] = {}
_RATE_LIMIT = 20  # requests per minute per IP

def _client_ip() -> str:
    """Return the best-available client IP. By default trust the immediate
    peer's `request.remote_addr` (Caddy/Proxmox/whatever's on :5173 directly).
    If the operator sets TRUST_PROXY=1 in the environment, honor
    X-Forwarded-For (right-most entry, since Caddy appends on each hop). The
    previous version trusted X-Forwarded-For unconditionally, which let any
    client spoof the limiter key and bypass the rate cap.
    """
    trust_proxy = os.environ.get("TRUST_PROXY", "").lower() in ("1", "true", "yes")
    if trust_proxy:
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            # Last entry is the original client per the X-Forwarded-For spec
            last = xff.split(",")[-1].strip()
            if last:
                return last
    return request.remote_addr or "unknown"


def rate_limited(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        ip = _client_ip()
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

# Cap the in-memory job map. Without this, a long-running photogen instance
# serving 100 generations/day accumulates thousands of completed job records
# for free (and they're never GC'd by `running` status). When the cap is
# exceeded, the oldest *completed* (done|error) job is evicted; running jobs
# are never evicted. Cap is overridable via CREATIVE_MAX_JOBS env var.
_MAX_JOBS = int(os.environ.get("CREATIVE_MAX_JOBS", "500"))
_JOB_TTL_SECONDS = 24 * 60 * 60  # evict completed jobs older than 24h regardless


def _job_id() -> str:
    return "job_" + uuid.uuid4().hex[:12]


def _evict_old_jobs():
    """Called under _jobs_lock. Evict oldest completed jobs to keep the map
    under _MAX_JOBS, and drop any completed job older than _JOB_TTL_SECONDS.
    """
    now = time.time()
    # 1) Time-based sweep first — drop stale completed jobs regardless of count
    stale = [
        jid for jid, j in _jobs.items()
        if j.get("finished_at") and (now - j["finished_at"]) > _JOB_TTL_SECONDS
    ]
    for jid in stale:
        _jobs.pop(jid, None)
    # 2) Size cap — drop oldest completed jobs (running jobs are protected)
    if len(_jobs) <= _MAX_JOBS:
        return
    completed = sorted(
        ((jid, j) for jid, j in _jobs.items() if j.get("status") in ("done", "error")),
        key=lambda kv: kv[1].get("finished_at") or kv[1].get("started_at") or 0,
    )
    while len(_jobs) > _MAX_JOBS and completed:
        jid, _ = completed.pop(0)
        _jobs.pop(jid, None)


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
                _evict_old_jobs()
        except Exception as e:
            with _jobs_lock:
                _jobs[job_id]["status"] = "error"
                _jobs[job_id]["error"] = str(e)
                _jobs[job_id]["finished_at"] = time.time()
                _evict_old_jobs()

    with _jobs_lock:
        _jobs[job_id] = {
            "status": "running",
            "started_at": time.time(),
            "result": None,
            "error": None,
            "finished_at": None,
        }
        _evict_old_jobs()
    t = threading.Thread(target=_worker, daemon=True)
    t.start()


# ── Frontend templates & static files ──────────────────────────────────
# Templates live in ./templates/ (landing.html, app.html)
# CSS + JS live in ./static/ (served at /static/*)
LANDING_TEMPLATE = "landing.html"
APP_TEMPLATE = "app.html"


# ── Frontend routes ────────────────────────────────────────────────────


@app.route("/")
def index():
    """Marketing landing page. No auth, no editor. Shippable public surface."""
    return render_template(LANDING_TEMPLATE)


@app.route("/app")
def app_editor():
    """The editor. BYOK required for generation."""
    return render_template(APP_TEMPLATE)


# ── API Routes ──────────────────────────────────────────────────────────


@app.route("/api/generate", methods=["POST"])
@rate_limited
def api_generate():
    data = request.json or {}
    prompt = data.get("prompt", "").strip()
    if not prompt:
        return jsonify({"error": "Prompt required"}), 400
    # Length cap runs before figma context expansion so a huge figma
    # payload can't sneak past via concatenation.
    cap = _enforce_prompt_length(prompt)
    if cap is not None:
        return cap

    # ── BYOK gate (applies to both sync and async paths) ──
    api_key, err = _require_api_key()
    if err is not None:
        return err

    mode = data.get("mode", "direct")
    tier = data.get("tier", "balanced")
    aspect = data.get("aspect_ratio", "16:9")
    smart = True
    variations = int(data.get("variations", 1))
    session_id = data.get("session_id", new_session_id())
    figma_url = data.get("figma_url")

    # ── Server-side cost guardrail ──
    guard = enforce_daily_limit(variations, tier)
    if guard is not None:
        return guard

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


_SSRF_BLOCKED_SCHEMES = frozenset(("", "file", "ftp", "gopher", "ldap", "dict", "data", "javascript"))


def _is_safe_export_url(url: str) -> bool:
    """Reject anything that isn't a public http(s) URL pointing at a routable host.

    Blocks SSRF to loopback, link-local, private RFC1918 ranges, multicast,
    unspecified, and reserved IPv6 ranges. The /image/... path on our own
    server is the only legit use, so we also allow it explicitly.
    """
    from urllib.parse import urlparse
    if not url:
        return False
    # Allow our own /image/ paths (same-origin only — no host tricks)
    if url.startswith("/image/") and "://" not in url and "\n" not in url and "\r" not in url:
        return True
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme.lower() not in ("http", "https"):
        return False
    if p.scheme.lower() in _SSRF_BLOCKED_SCHEMES:
        return False
    host = (p.hostname or "").lower()
    if not host:
        return False
    # Strip IPv6 brackets if any
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    # Resolve and inspect every address (hostnames can resolve to private IPs)
    import ipaddress, socket
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False
    return True


@app.route("/api/export-zip", methods=["POST"])
@rate_limited
def api_export_zip():
    import io, zipfile, urllib.request
    data = request.json or {}
    urls = data.get("urls", [])
    if not urls:
        return jsonify({"error": "No URLs provided"}), 400
    # SSRF gate: reject anything not on the public internet
    rejected = [u for u in urls if not _is_safe_export_url(u)]
    if rejected:
        return jsonify({
            "error": "Rejected non-public or unsafe URL(s)",
            "rejected": rejected[:5],
        }), 400
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
    cap = _enforce_prompt_length(prompt)
    if cap is not None:
        return cap
    api_key, err = _require_api_key()
    if err is not None:
        return err

    # ── Server-side cost guardrail ──
    composite_tier = request.form.get("tier", "balanced")
    guard = enforce_daily_limit(1, composite_tier)
    if guard is not None:
        return guard

    tmp_dir = DATA_DIR / "uploads"
    tmp_dir.mkdir(exist_ok=True)
    product_path = tmp_dir / f"product_{int(time.time())}_{f.filename}"
    f.save(str(product_path))

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
        rel_path = img_url.replace("/image/", "", 1)
        safe = _safe_output_relpath(rel_path)
        if not safe:
            return jsonify({"error": "Image path is invalid or outside the output directory"}), 400
        src_path = safe
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
    # NB: api_export doesn't call _require_api_key() (export uses a local
    # PIL pipeline and isn't billed), so _get_api_key() is the correct call.
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
        rel_path = img_url.replace("/image/", "", 1)
        img_path = _safe_output_relpath(rel_path)
        if not img_path:
            return jsonify({"error": "Image path is invalid or outside the output directory"}), 400
    # Case 2: Uploaded image
    elif "image" in request.files:
        f = request.files["image"]
        tmp_dir = DATA_DIR / "uploads"
        tmp_dir.mkdir(exist_ok=True)
        img_path = tmp_dir / f"qc_{int(time.time())}_{f.filename}"
        f.save(str(img_path))
    else:
        return jsonify({"error": "Image required"}), 400

    qc = run_cli_qc(str(img_path), api_key)
    return jsonify({"message": f"QC Score: {qc['quality_score']}/10", "qc": qc})


@app.route("/api/figma", methods=["POST"])
@rate_limited
def api_figma():
    api_key, err = _require_api_key()
    if err is not None:
        return err
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
    api_key, err = _require_api_key()
    if err is not None:
        return err
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

    images = run_cli_refine(image_path, full_changes, api_key, tier)
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
    api_key, err = _require_api_key()
    if err is not None:
        return err
    data = request.json or {}
    prompt = data.get("prompt", "").strip()
    if not prompt:
        return jsonify({"error": "Prompt required"}), 400
    cap = _enforce_prompt_length(prompt)
    if cap is not None:
        return cap

    count = int(data.get("count", 4))
    count = max(1, min(8, count))
    tier = data.get("tier", "balanced")
    aspect = data.get("aspect_ratio", "1:1")
    session_id = data.get("session_id", new_session_id())

    # ── Server-side cost guardrail ──
    guard = enforce_daily_limit(count, tier)
    if guard is not None:
        return guard

    # Handle optional reference image upload
    input_image = None
    if "image" in request.files:
        f = request.files["image"]
        tmp_dir = DATA_DIR / "uploads"
        tmp_dir.mkdir(exist_ok=True)
        input_image = str(tmp_dir / f"variations_ref_{int(time.time())}_{f.filename}")
        f.save(input_image)

    images, session_key = run_cli_variations(api_key,
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


# ── Scene-set endpoint: one product, 5 scene types, 5 outputs in parallel ──
# This is the Riverflow-style "wow" — upload a product, get one of each
# scene type back in a single click. No client-side loop required.
_SCENE_PROMPTS = {
    "inhand":   "Close-up of a hand holding the product, natural skin tone, soft daylight from window, shallow depth of field, the hand fills the lower half of the frame, product in sharp focus, editorial product photography, 85mm lens",
    "studio":   "Product on a clean seamless studio backdrop, controlled soft-box lighting from upper left, soft natural shadow underneath, perfectly centered, no distractions, ecommerce-grade product photography, color-calibrated white background, sharp from edge to edge",
    "action":   "Product in mid-use, dynamic action moment — pouring, opening, applying, or being squeezed — motion implied by blur on liquid or cap, frozen peak moment, high shutter speed feel, dramatic side lighting, lifestyle energy, candid and authentic",
    "lifestyle": "Product in a real-world lifestyle scene with a person, natural environment (cafe, kitchen, gym, park, or shelf), warm available light, authentic and unstaged feeling, the person is mid-activity, product naturally placed, shot in documentary style, human warmth",
    "withprops": "Product styled with complementary props that suggest its category and use — fresh ingredients, accessories, tools, or pairing items — arranged on a textured surface (marble, wood, linen), overhead 45 degree angle, editorial flatlay composition, warm natural light, the product is the focal point with props supporting",
}
_SCENE_ASPECTS = {
    "inhand":   "4:5",
    "studio":   "1:1",
    "action":   "4:5",
    "lifestyle": "4:5",
    "withprops": "1:1",
}
_SCENE_LABELS = {
    "inhand":   "In-hand",
    "studio":   "Studio",
    "action":   "Action",
    "lifestyle": "Lifestyle",
    "withprops": "With props",
}


@app.route("/api/scene-set", methods=["POST"])
@rate_limited
def api_scene_set():
    """One product → 5 images, one per scene type, generated in parallel threads.

    Form fields:
      - product: the product image file (required)
      - tier: 'fast' | 'balanced' | 'quality' | 'ultra' (default: balanced)
      - session_id: optional session id

    Returns JSON {images: [...], message, session_id} where each image has
    scene (inhand|studio|action|lifestyle|withprops), label, url, cost, model.
    """
    api_key, err = _require_api_key()
    if err is not None:
        return err

    if "product" not in request.files:
        return jsonify({"error": "Product image required (form field 'product')"}), 400

    f = request.files["product"]
    if not f or not f.filename:
        return jsonify({"error": "Empty product upload"}), 400
    # Filename-based mime sniff (works across storage backends; some
    # FileStorage wrappers raise on .type access for in-memory uploads).
    fname = f.filename or ""
    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
    if ext not in ("png", "jpg", "jpeg", "webp", "gif", "bmp"):
        return jsonify({"error": "Product must be PNG, JPG, WEBP, GIF, or BMP"}), 400

    tier = request.form.get("tier", "balanced")
    if tier not in _TIER_MODEL:
        tier = "balanced"
    session_id = request.form.get("session_id", new_session_id())

    # Cost guardrail: 5 images worst-case
    guard = enforce_daily_limit(5, tier)
    if guard is not None:
        return guard

    # Save the uploaded product once
    tmp_dir = DATA_DIR / "uploads"
    tmp_dir.mkdir(exist_ok=True)
    product_path = tmp_dir / f"sceneset_{int(time.time())}_{f.filename}"
    f.save(str(product_path))

    # Use a thread pool to run all 5 scenes in parallel
    images: List[Dict] = []
    images_lock = threading.Lock()
    scenes = list(_SCENE_PROMPTS.keys())

    def _run_scene(scene_key: str):
        prompt = _SCENE_PROMPTS[scene_key]
        aspect = _SCENE_ASPECTS[scene_key]
        try:
            imgs = run_cli_composite(prompt, str(product_path), api_key, aspect)
            if imgs and "error" not in imgs[0]:
                img = imgs[0]
                with images_lock:
                    images.append({
                        "scene": scene_key,
                        "label": _SCENE_LABELS[scene_key],
                        "url": img.get("url", ""),
                        "path": img.get("path", ""),
                        "name": img.get("name", ""),
                        "cost": img.get("cost", 0),
                        "model": img.get("model", ""),
                        "ratio": aspect,
                    })
                    add_entry(
                        session_id,
                        {
                            "type": "sceneset",
                            "prompt": prompt[:100],
                            "cost": img.get("cost", 0),
                            "image_url": img.get("url", ""),
                            "model": img.get("model", ""),
                            "note": _SCENE_LABELS[scene_key],
                        },
                    )
        except Exception as e:
            # Silently skip failed scenes so the user still gets 4 of 5
            print(f"[sceneset] {scene_key} failed: {e}", file=sys.stderr)

    threads = [threading.Thread(target=_run_scene, args=(s,), daemon=True) for s in scenes]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=300)  # each scene up to 5 min

    # Order the output to match scene order (Riverflow-style bento)
    images.sort(key=lambda x: scenes.index(x["scene"]) if x["scene"] in scenes else 99)

    total_cost = sum(img.get("cost", 0) for img in images)
    return jsonify({
        "message": f"Generated {len(images)}/{len(scenes)} scene(s)",
        "images": images,
        "session_id": session_id,
        "total_cost": total_cost,
        "scenes_requested": scenes,
    })


@app.route("/api/variations/<session_key>/refine", methods=["POST"])
@rate_limited
def api_variations_refine(session_key):
    api_key, err = _require_api_key()
    if err is not None:
        return err
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
    api_key, err = _require_api_key()
    if err is not None:
        return err
    data = request.json or {}
    prompt = data.get("prompt", "").strip()
    if not prompt:
        return jsonify({"error": "Prompt required"}), 400
    cap = _enforce_prompt_length(prompt)
    if cap is not None:
        return cap

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

    images, sess = run_cli_chat_turn(api_key,
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


@app.route("/api/whoami", methods=["GET"])
@rate_limited
def api_whoami():
    """Tell the frontend the BYOK/fallback status.

    The frontend uses this to decide whether to show the key input as required
    or as a convenience, and whether server fallback will work if no key is set.
    """
    user_key = request.headers.get("X-API-Key", "").strip()
    return jsonify({
        "byok": bool(user_key),
        "user_supplied_key": bool(user_key),
        "server_has_fallback": bool(SERVER_API_KEY),
        "fallback_enabled": bool(ALLOW_SERVER_FALLBACK and SERVER_API_KEY),
        "byok_required": not (ALLOW_SERVER_FALLBACK and SERVER_API_KEY),
        "version": __version__,
    })


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
        '.nav { margin-bottom:24px; display:flex; gap:8px; flex-wrap:wrap; }'
        '.nav a { font-size:0.85rem; color:var(--text2); padding:8px 12px; min-height:32px; display:inline-flex; align-items:center; border-radius:6px; }'
        '.nav a:hover { color:var(--text); background:rgba(255,255,255,0.05); text-decoration:none; }'
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
        '<tr><td>gemini-3.1-flash-image-preview (1K)</td><td>$0.045</td></tr>'
        '<tr><td>gemini-3.1-flash-image-preview (2K)</td><td>$0.090</td></tr>'
        '<tr><td>gemini-3-pro-image-preview (2K)</td><td>$0.240</td></tr>'
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
        '.nav { margin-bottom:24px; display:flex; gap:8px; flex-wrap:wrap; }'
        '.nav a { font-size:0.85rem; color:var(--text2); padding:8px 12px; min-height:32px; display:inline-flex; align-items:center; border-radius:6px; }'
        '.nav a:hover { color:var(--text); background:rgba(255,255,255,0.05); text-decoration:none; }'
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
