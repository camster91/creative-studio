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
from datetime import datetime, timedelta
from typing import Optional, List, Dict

from flask import Flask, render_template, request, jsonify, send_from_directory, send_file

from figma_utils import parse_figma_url, fetch_figma_context, enhance_prompt_with_figma
from creative_studio_app.assets import (
    build_pin_prompt,
    image_url as _asset_image_url,
    new_pin_id as pin_id,
    pin_to_region,
    safe_output_relpath as _resolve_output_path,
)
from creative_studio_app.costs import (
    check_daily_limit as _cost_limit_check,
    cost_for_tier as _tier_cost,
    load_costs as _load_costs,
    save_costs as _save_costs,
    track_cost as _record_cost,
)
from creative_studio_app.informational import (
    render_docs as _render_docs,
    render_history as _render_history,
    render_status as _render_status,
)
from creative_studio_app import auth as _auth_service
from creative_studio_app import projects as _project_service
from creative_studio_app import generation as _generation_service
from creative_studio_app import delivery as _delivery_service
from creative_studio_app import iterations as _iteration_service
from creative_studio_app.jobs import (
    evict_old_jobs as _evict_jobs,
    job_id as _new_job_id,
    run_job_background as _start_job,
)
from creative_studio_app.rate_limit import (
    client_ip as _client_ip,
    create_rate_limiter,
)


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


# Max bytes for a user-supplied uploaded filename. Defense against
# a malicious client sending `../../../etc/cron.d/evil` as the
# Content-Disposition filename. We strip path components and cap
# length; the actual on-disk name is `prefix_<random>.<ext>` (set by
# each handler) so the sanitized name is just for logging.
_MAX_UPLOAD_FILENAME_BYTES = 200

# Allowed image extensions for the scene-set endpoint. Other
# endpoints don't strictly need this — they pass the raw file to
# the Gemini CLI which accepts any image — but we keep one allowlist
# here so a handler that does extra mime validation can re-use it.
_IMAGE_EXTS = ("png", "jpg", "jpeg", "webp", "gif", "bmp")


def _safe_filename(filename: str, max_bytes: int = _MAX_UPLOAD_FILENAME_BYTES) -> str:
    """Return a filename safe to embed in a save path.

    - Strips any directory components (`/`, `\\`, `..` segments collapse to nothing)
    - Drops NUL bytes and control chars
    - Caps length to max_bytes (UTF-8 encoded)
    - Rejects names that are entirely dots (`..`, `...`) so a client
      can't bypass Path.name's single-component `..` survival
    - Returns '' if the result is empty after sanitization
    """
    if not filename:
        return ""
    # Treat backslash the same as forward slash (Windows-style paths
    # must be stripped even on POSIX — otherwise `..\..\..\evil` survives
    # as a single "filename" with backslashes intact).
    normalized = filename.replace("\\", "/")
    # Path.name strips everything before the last separator. Works on both
    # POSIX and Windows because both are treated the same by pathlib.
    name = Path(normalized).name
    # Belt-and-suspenders: remove any remaining path separators or NULs.
    name = name.replace("\x00", "").replace("/", "").replace("\\", "")
    # Path("..").name == ".." (single-component parent). Reject any name
    # that is composed entirely of dots — those would cause shutil/os.path
    # to resolve as parent dirs in some downstream code.
    if name and set(name) <= {"."}:
        return ""
    # Cap length. We use bytes, not chars, because Path.name returns a
    # str but a Unicode name could still be huge in bytes.
    if len(name.encode("utf-8")) > max_bytes:
        # Truncate safely on a char boundary
        truncated = name.encode("utf-8", errors="replace")[:max_bytes]
        name = truncated.decode("utf-8", errors="replace")
    return name


def _safe_pin_id(pin_id: str) -> str:
    """Validate a pin_id from the URL. Must be the 8-hex shape produced
    by pin_id() (uuid4 hex prefix). Reject anything else so a malicious
    caller can't sweep pins.json with a wildcard. Returns the lowercased
    id, or '' if invalid.
    """
    if not pin_id or len(pin_id) > 16:
        return ""
    lowered = pin_id.lower()
    return lowered if all(c in "0123456789abcdef" for c in lowered) else ""

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
    `api_key, err, used_trial_credit = _require_api_key(); if err: return err`.

    Auth resolution order (WS-2):
    1. X-API-Key header (per-request BYOK) — preferred, cost billed to user
    2. Server fallback key (CREATIVE_ALLOW_SERVER_FALLBACK=true +
       GEMINI_API_KEY env) — for the public demo
    3. Signed-in user without a key → consume a trial credit
       (`_use_trial_credit`). Returns the SERVER_API_KEY so the
       generation still works, but with a flag the caller can use
       to surface "you used 1 of 5 trial credits" in the response.
       If credits are exhausted, returns 402 with the trial-expired
       message.

    Returns: (key, err, used_trial_credit) where used_trial_credit
    is True if we burned a credit on this call. The third value
    is None if err is not None.
    """
    key: Optional[str] = _get_api_key()
    if key:
        return key, None, False

    # Try the trial-credit fallback
    sess = _current_session()
    if sess and sess.get("credits_remaining", 0) > 0:
        ok, remaining = _use_trial_credit(sess["user_id"])
        if ok:
            # Use the server fallback key for the actual generation
            if SERVER_API_KEY:
                return SERVER_API_KEY, None, True
            # Server has no fallback key but user is signed in
            # with credits — weird state, but we should still
            # surface a helpful error rather than silently 402.
            return None, (
                jsonify({
                    "error": "Trial credit burned but no server-side API key configured",
                    "message": "Set CREATIVE_ALLOW_SERVER_FALLBACK=true and GEMINI_API_KEY on the host.",
                }),
                500,
            ), None

    # No key, no credits — return 402
    message = (
        "Add your Gemini API key in the editor sidebar, "
        "or sign up for a free Photogen account and use one of your 5 trial credits. "
        "We don't store or train on your key — cost is billed directly to your Google account."
    )
    if sess and sess.get("credits_remaining", 0) == 0:
        message = (
            "Your 5 free trial credits are used up. "
            "Add your own Gemini API key in the editor sidebar to keep going. "
            "We don't store or train on your key — cost is billed directly to your Google account."
        )
    return None, (
        jsonify({
            "error": "BYOK or sign-in required",
            "message": message,
        }),
        402,
    ), None


def _trial_credit_response_meta(user_email: str, remaining: int) -> dict:
    """Return a small dict that callers can splat into their response to
    tell the frontend that a trial credit was burned. The frontend
    can update the credits badge without a refresh."""
    return {
        "trial_credit_used": True,
        "credits_remaining": remaining,
        "user_email": user_email,
    }



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
    return _load_costs(COST_DB)


def save_costs(data: dict):
    _save_costs(COST_DB, data)


def track_cost(model: str, resolution: str = "1K", count: int = 1):
    """Charge the user for `count` images at the given model/resolution tier.

    Atomic read-mutate-write under _json_lock so concurrent calls don't lose
    increments. The cost guardrail uses the same lock via _try_charge_costs()
    so the limit is enforced exactly once per request.
    """
    return _record_cost(COST_DB, COSTS, model, resolution, count, _json_lock)


def _check_daily_limit(est_count: int = 1, tier: str = "balanced"):
    """Atomically check whether `est_count` images at `tier` would push today's
    spend past the CREATIVE_DAILY_LIMIT cap.

    Returns:
        None if the request is allowed to proceed.
        A (response, status) tuple to short-circuit with 429.

    The check is held under _json_lock so two concurrent callers cannot
    both pass the limit and both proceed.
    """
    try:
        daily_limit = float(os.environ.get("CREATIVE_DAILY_LIMIT", "5"))
    except (TypeError, ValueError):
        daily_limit = 5.0
    rejection = _cost_limit_check(
        COST_DB,
        estimated_count=est_count,
        tier=tier,
        daily_limit=daily_limit,
        tier_models=_TIER_MODEL,
        price_card=COSTS,
        lock=_json_lock,
    )
    return (jsonify(rejection), 429) if rejection else None


# Backward-compat alias. Old callers using enforce_daily_limit() as a pure
# check (no charge) still work — it now serializes on the same lock as the
# post-generation track_cost() so the limit can't be bypassed by racing
# requests. New code should prefer _check_daily_limit.
#
# IMPORTANT: this MUST be the only `def enforce_daily_limit` in the
# module. A previous version of this fix left a duplicate (unlocked)
# definition lower in the file, which Python silently shadowed the
# locked one — and the TOCTOU bypass came back. The regression test
# test_enforce_daily_limit_alias_uses_locked_version checks for the
# absence of any second definition.
def enforce_daily_limit(est_count: int = 1, tier: str = "balanced"):
    return _check_daily_limit(est_count, tier)


def cost_for_tier(tier: str) -> float:
    """Estimated per-image cost for a quality tier."""
    return _tier_cost(tier, _TIER_MODEL, COSTS)


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
    return _asset_image_url(path, OUTPUT_DIR)


def _safe_output_relpath(rel_path: str) -> Optional[Path]:
    """Resolve a user-supplied /image/<rel> tail against OUTPUT_DIR, rejecting
    any path that escapes it (the same guard serve_image() already uses).
    Returns the resolved Path if safe, None if traversal or non-existent.
    """
    return _resolve_output_path(rel_path, OUTPUT_DIR)


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


# ─── Image generation wrappers ────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent
SCRIPT_PATH = str(SCRIPT_DIR / "creative_studio.py")

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
    return _generation_service.generate(
        prompt,
        mode,
        api_key,
        tier,
        aspect,
        smart,
        input_image=input_image,
        variations=variations,
        output_dir=OUTPUT_DIR,
        script_path=SCRIPT_PATH,
        python_executable=sys.executable,
        tier_models=_TIER_MODEL,
        run=subprocess.run,
        record_cost=track_cost,
        to_image_url=image_url,
    )



def run_cli_composite(
    prompt: str,
    product_path: str,
    api_key: str,
    aspect: str,
    tier: str = "quality",
    name_suffix: str = "",
) -> List[Dict]:
    return _generation_service.composite(
        prompt,
        product_path,
        api_key,
        aspect,
        tier,
        name_suffix=name_suffix,
        output_dir=OUTPUT_DIR,
        script_path=SCRIPT_PATH,
        python_executable=sys.executable,
        run=subprocess.run,
        record_cost=track_cost,
        to_image_url=image_url,
    )



def run_cli_export(source_path: str, presets: str, api_key: str) -> List[Dict]:
    return _delivery_service.export_images(
        source_path,
        presets,
        api_key,
        launch_script=Path(__file__).parent.parent / "launch.sh",
        output_dir=OUTPUT_DIR,
        run=subprocess.run,
        to_image_url=image_url,
    )



def run_cli_qc(image_path: str, api_key: str) -> dict:
    return _delivery_service.run_qc(
        image_path,
        api_key,
        launch_script=Path(__file__).parent.parent / "launch.sh",
        run=subprocess.run,
    )



def run_cli_refine(image_path: str, changes: str, api_key: str, tier: str) -> List[Dict]:
    return _iteration_service.refine(
        image_path,
        changes,
        api_key,
        tier,
        output_dir=OUTPUT_DIR,
        launch_script=Path(__file__).parent.parent / "launch.sh",
        run=subprocess.run,
        record_cost=track_cost,
        to_image_url=image_url,
    )


_VARIATION_SUFFIXES = _iteration_service.VARIATION_SUFFIXES


def run_cli_variations(
    prompt: str,
    api_key: str,
    count: int,
    tier: str,
    aspect: str,
    input_image: Optional[str] = None,
) -> tuple[List[Dict], str]:
    return _iteration_service.variations(
        prompt,
        api_key,
        count,
        tier,
        aspect,
        input_image=input_image,
        output_dir=OUTPUT_DIR,
        script_path=SCRIPT_PATH,
        python_executable=sys.executable,
        tier_models=_TIER_MODEL,
        run=subprocess.run,
        record_cost=track_cost,
        to_image_url=image_url,
    )


def run_cli_refine_from_variation(
    session_key: str,
    pick_index: int,
    changes: str,
    tier: str,
    api_key: str,
) -> List[Dict]:
    return _iteration_service.refine_variation(
        session_key,
        pick_index,
        changes,
        tier,
        api_key,
        output_dir=OUTPUT_DIR,
        script_path=SCRIPT_PATH,
        python_executable=sys.executable,
        tier_models=_TIER_MODEL,
        run=subprocess.run,
        record_cost=track_cost,
        to_image_url=image_url,
    )



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

# ── Auth and project persistence services ───────────────────────────────
import zipfile as _zipfile
import io as _io
import json as _json_projects

AUTH_DB = DATA_DIR / "users.db"
_SESSION_DAYS = 7
_MAGIC_LINK_MINUTES = 60
_FREE_TRIAL_CREDITS = 5

_PROJECTS_MAX_PER_USER = 200
_PROJECT_NAME_MAX = 200
_GENERATION_URL_MAX = 2000
_GENERATION_PROMPT_MAX = 4000


def _auth_db():
    return _auth_service.connect(AUTH_DB)


def _init_auth_schema():
    _auth_service.init_schema(AUTH_DB)


_init_auth_schema()


def _now_iso() -> str:
    return _auth_service.now_iso()


def _create_magic_link_token(email: str) -> str:
    return _auth_service.create_magic_link(AUTH_DB, email, _MAGIC_LINK_MINUTES)


def _consume_magic_link(token: str) -> dict | None:
    return _auth_service.consume_magic_link(
        AUTH_DB,
        token,
        session_days=_SESSION_DAYS,
        free_trial_credits=_FREE_TRIAL_CREDITS,
    )


def _session_from_cookie(cookie: str) -> dict | None:
    return _auth_service.session_from_token(AUTH_DB, cookie)


def _current_session() -> dict | None:
    token = request.headers.get("X-Session-Token", "").strip()
    return _session_from_cookie(token) if token else None


def _use_trial_credit(user_id: str) -> tuple[bool, int]:
    return _auth_service.use_trial_credit(AUTH_DB, user_id)


def _parse_generations_json(raw: str) -> list:
    return _project_service.parse_generations(raw)


def _serialize_project_row(row, include_generations: bool = True) -> dict:
    return _project_service.serialize(row, include_generations)


def _create_project(user_id: str, name: str, source_session_id: str = None) -> dict:
    return _project_service.create(
        AUTH_DB,
        user_id,
        name,
        source_session_id,
        max_projects=_PROJECTS_MAX_PER_USER,
        name_max=_PROJECT_NAME_MAX,
    )


def _get_project(project_id: str) -> dict:
    return _project_service.get(AUTH_DB, project_id)


def _get_project_for_user(project_id: str, user_id: str) -> dict:
    return _project_service.get(AUTH_DB, project_id, user_id)


def _list_projects_for_user(user_id: str, include_generations: bool = False) -> list:
    return _project_service.list_for_user(AUTH_DB, user_id, include_generations)


def _add_generation_to_project(
    project_id: str,
    user_id: str,
    url: str,
    prompt: str,
    cost: float = 0,
    model: str = "",
    ratio: str = "",
) -> dict:
    return _project_service.add_generation(
        AUTH_DB,
        project_id,
        user_id,
        url=url,
        prompt=prompt,
        cost=cost,
        model=model,
        ratio=ratio,
        url_max=_GENERATION_URL_MAX,
        prompt_max=_GENERATION_PROMPT_MAX,
    )


def _delete_project(project_id: str, user_id: str) -> bool:
    return _project_service.delete(AUTH_DB, project_id, user_id)



# ── Simple in-memory rate limiter ───────────────────────────────────────
_request_log: Dict[str, list] = {}
_request_log_lock = threading.Lock()
_RATE_LIMIT = 20  # requests per minute per IP

# Cap the number of distinct IPs the rate limiter tracks. Without this,
# a botnet with rotating IPs (or a single IPv6-rich NAT) can grow the
# dict unbounded — each entry is ~200 bytes, so 1M IPs = 200MB held
# in the worker for no reason. When the cap is hit, the IP with the
# oldest activity (oldest last-timestamp) is evicted. The 60s timestamp
# purge inside the wrapper means that, in practice, the cap also
# drops stale entries naturally — the explicit eviction is the
# safety net for the long-tail of IPs that touch once and never again.
_MAX_TRACKED_IPS = 50000

rate_limited = create_rate_limiter(
    lambda: _RATE_LIMIT,
    lambda: _MAX_TRACKED_IPS,
    _request_log,
    _request_log_lock,
)

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
    return _new_job_id()


def _evict_old_jobs():
    """Called under _jobs_lock. Evict oldest completed jobs to keep the map
    under _MAX_JOBS, and drop any completed job older than _JOB_TTL_SECONDS.
    """
    _evict_jobs(
        _jobs,
        max_jobs=_MAX_JOBS,
        ttl_seconds=_JOB_TTL_SECONDS,
    )


def _run_job_background(
    job_id: str,
    fn,
    *args,
    **kwargs,
):
    """Compatibility wrapper around the independently tested job service."""
    _start_job(
        job_id,
        fn,
        *args,
        jobs=_jobs,
        lock=_jobs_lock,
        evict=_evict_old_jobs,
        **kwargs,
    )


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
    api_key, err, used_trial_credit = _require_api_key()
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

    response_payload = {
        "message": f"Generated {len(images)} image(s)",
        "images": images,
        "session_id": session_id,
    }
    if used_trial_credit:
        # Surface the credit burn so the frontend can update the badge
        # without a refresh. Look up the user's current remaining count.
        sess = _current_session()
        if sess:
            response_payload.update({
                "trial_credit_used": True,
                "credits_remaining": sess["credits_remaining"],
            })
    return jsonify(response_payload)


@app.route("/api/jobs/<job_id>", methods=["GET"])
@rate_limited
def api_job_status(job_id):
    api_key, err, used_trial_credit = _require_api_key()
    if err is not None:
        return err
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
    """Check if a Gemini API key is valid.

    Sends the key in the `x-goog-api-key` header (NOT as a ?key= query
    parameter) so it doesn't appear in Google access logs, browser
    history, or any intermediate proxy logs. Also never echoes the
    key in error responses — the client gets a sanitized message.
    """
    data = request.json or {}
    key = data.get("key", "").strip()
    if not key:
        return jsonify({"error": "Key required"}), 400
    if not key.startswith("AIza"):
        return jsonify({"error": "Invalid format — Gemini keys start with AIza..."}), 400
    if len(key) > 200:
        # Gemini keys are ~40 chars. 200 is a generous upper bound that
        # also catches runaway clients trying to use this endpoint as
        # an arbitrary HTTPS proxy.
        return jsonify({"error": "Key too long"}), 400

    # Probe by listing models with the key in a header, not a query string.
    import urllib.request, urllib.error
    req = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/models",
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                return jsonify({"valid": True, "message": "Key is valid"})
    except urllib.error.HTTPError as e:
        if e.code == 400 or e.code == 403:
            # 400 = "API key not valid" / 403 = "permission denied". Both
            # mean the key is bad or the API isn't enabled. Don't echo
            # Google's body — it can echo the key back.
            return jsonify({"valid": False, "error": "Invalid API key"}), 200
        # 429 = rate limit, 5xx = Google down. Neither is "invalid key".
        return jsonify({"valid": False, "error": f"HTTP {e.code}"}), 200
    except Exception:
        # Don't include str(e) in the response — some exception classes
        # include the URL (and therefore the key) in their string form.
        return jsonify({"valid": False, "error": "Network error"}), 200

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
    api_key, err, used_trial_credit = _require_api_key()
    if err is not None:
        return err

    # ── Server-side cost guardrail ──
    composite_tier = request.form.get("tier", "balanced")
    guard = enforce_daily_limit(1, composite_tier)
    if guard is not None:
        return guard

    tmp_dir = DATA_DIR / "uploads"
    tmp_dir.mkdir(exist_ok=True)
    product_path = tmp_dir / f"product_{int(time.time())}_{_safe_filename(f.filename)}"
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
        src_path = tmp_dir / f"export_{int(time.time())}_{_safe_filename(f.filename)}"
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
        img_path = tmp_dir / f"qc_{int(time.time())}_{_safe_filename(f.filename)}"
        f.save(str(img_path))
    else:
        return jsonify({"error": "Image required"}), 400

    qc = run_cli_qc(str(img_path), api_key)
    return jsonify({"message": f"QC Score: {qc['quality_score']}/10", "qc": qc})


@app.route("/api/figma", methods=["POST"])
@rate_limited
def api_figma():
    api_key, err, used_trial_credit = _require_api_key()
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
    api_key, err, used_trial_credit = _require_api_key()
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
    api_key, err, used_trial_credit = _require_api_key()
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
        input_image = str(tmp_dir / f"variations_ref_{int(time.time())}_{_safe_filename(f.filename)}")
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
    api_key, err, used_trial_credit = _require_api_key()
    if err is not None:
        return err

    if "product" not in request.files:
        return jsonify({"error": "Product image required (form field 'product')"}), 400

    f = request.files["product"]
    if not f or not f.filename:
        return jsonify({"error": "Empty product upload"}), 400
    # Filename-based mime sniff (works across storage backends; some
    # FileStorage wrappers raise on .type access for in-memory uploads).
    # Note: strip any path components first so a filename like
    # `../../etc/passwd.png` doesn't both pass the extension check AND
    # smuggle a path-traversal into the save path. The save path uses
    # _safe_filename() below which strips a second time for defense in
    # depth.
    fname = _safe_filename(f.filename)
    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
    if ext not in _IMAGE_EXTS:
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
    product_path = tmp_dir / f"sceneset_{int(time.time())}_{_safe_filename(f.filename)}"
    f.save(str(product_path))

    # Use a thread pool to run all 5 scenes in parallel
    images: List[Dict] = []
    images_lock = threading.Lock()
    scenes = list(_SCENE_PROMPTS.keys())

    def _run_scene(scene_key: str):
        prompt = _SCENE_PROMPTS[scene_key]
        aspect = _SCENE_ASPECTS[scene_key]
        try:
            # Pass the scene key as the name_suffix so the 5 parallel
            # scene-set threads can't collide on the timestamp-based
            # filename in run_cli_composite. Each scene gets a
            # distinct filename like composite-1700000000-inhand-<hex>.png
            # instead of all-5-writing-to-the-same composite-1700000000.png.
            imgs = run_cli_composite(
                prompt, str(product_path), api_key, aspect,
                name_suffix=scene_key,
            )
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
    api_key, err, used_trial_credit = _require_api_key()
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
        api_key=api_key,
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
    api_key, err, used_trial_credit = _require_api_key()
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
        input_image = str(tmp_dir / f"chat_ref_{int(time.time())}_{_safe_filename(f.filename)}")
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
    api_key, err, used_trial_credit = _require_api_key()
    if err is not None:
        return err
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
    api_key, err, used_trial_credit = _require_api_key()
    if err is not None:
        return err
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


# Pin image_path key is user-controlled. Cap it so a spammer can't
# fill pins.json with arbitrarily long keys. 2KB is enough for any
# real image_path the app uses.
_MAX_PIN_PATH_BYTES = 2 * 1024


def _safe_pin_path(image_path: str) -> str:
    """Validate and normalize a pin's image_path key.

    - Must be non-empty
    - Must be < _MAX_PIN_PATH_BYTES after UTF-8 encoding
    - Leading-slash normalization matches the existing read/write paths
    - Return '' if invalid (caller should return 400)
    """
    if not image_path:
        return ""
    if not image_path.startswith("/"):
        image_path = "/" + image_path
    if len(image_path.encode("utf-8")) > _MAX_PIN_PATH_BYTES:
        return ""
    return image_path


@app.route("/api/pins", methods=["POST"])
@rate_limited
def api_pins_add():
    api_key, err, used_trial_credit = _require_api_key()
    if err is not None:
        return err
    data = request.json or {}
    image_path = _safe_pin_path(data.get("image_path", ""))
    if not image_path:
        return jsonify({"error": "image_path required (max 2KB)"}), 400
    try:
        x = float(data.get("x", 0.5))
        y = float(data.get("y", 0.5))
    except (TypeError, ValueError):
        return jsonify({"error": "x and y must be numbers 0..1"}), 400
    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
        return jsonify({"error": "x and y must be in [0, 1]"}), 400
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "text required"}), 400
    if len(text.encode("utf-8")) > 1000:
        return jsonify({"error": "text too long (max 1000 bytes)"}), 400
    pins = load_pins(image_path)
    pins.append({"id": pin_id(), "x": x, "y": y, "text": text, "time": now_str()})
    save_pins(image_path, pins)
    return jsonify({"pins": pins})


@app.route("/api/pins/<path:image_path>", methods=["GET"])
@rate_limited
def api_pins_get(image_path):
    api_key, err, used_trial_credit = _require_api_key()
    if err is not None:
        return err
    image_path = _safe_pin_path(image_path)
    if not image_path:
        return jsonify({"error": "image_path required"}), 400
    return jsonify({"pins": load_pins(image_path)})


@app.route("/api/pins/<path:image_path>/<pin_id>", methods=["DELETE"])
@rate_limited
def api_pins_delete(image_path, pin_id):
    api_key, err, used_trial_credit = _require_api_key()
    if err is not None:
        return err
    image_path = _safe_pin_path(image_path)
    if not image_path:
        return jsonify({"error": "image_path required"}), 400
    safe_id = _safe_pin_id(pin_id)
    if not safe_id:
        return jsonify({"error": "pin_id must be hex (1-16 chars)"}), 400
    pins = [p for p in load_pins(image_path) if p.get("id") != safe_id]
    save_pins(image_path, pins)
    return jsonify({"pins": pins})


@app.route("/api/pins/<path:image_path>", methods=["DELETE"])
@rate_limited
def api_pins_clear(image_path):
    api_key, err, used_trial_credit = _require_api_key()
    if err is not None:
        return err
    image_path = _safe_pin_path(image_path)
    if not image_path:
        return jsonify({"error": "image_path required"}), 400
    save_pins(image_path, [])
    return jsonify({"pins": []})


@app.route("/api/sessions", methods=["GET"])
@rate_limited
def api_sessions():
    api_key, err, used_trial_credit = _require_api_key()
    if err is not None:
        return err
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
    api_key, err, used_trial_credit = _require_api_key()
    if err is not None:
        return err
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
    api_key, err, used_trial_credit = _require_api_key()
    if err is not None:
        return err
    costs = load_costs()
    costs["session_count"] = len(list(SESSIONS_DIR.glob("*.json")))
    today = datetime.now().strftime("%Y-%m-%d")
    costs["today"] = costs.get("by_date", {}).get(today, 0.0)
    return jsonify(costs)


# ── Waitlist (WS-1 — public landing page lead capture) ───────────────────
# Stores emails in a flat JSON file. No DB, no Stripe, no auth.
# Lives in /app/data/waitlist.json (DATA_DIR). The format is just a list
# of {email, source, ts} dicts — operator can grep / export as needed.
# When WS-2 (auth) ships, we add a `user_id` field on signup and link
# the waitlist record to the new user. When WS-6 (Stripe) ships, this
# endpoint can also be the entry point for a free-trial claim.
import re as _re

WAITLIST_FILE = DATA_DIR / "waitlist.json"
WAITLIST_FILE.touch(exist_ok=True)
if not WAITLIST_FILE.read_text().strip():
    WAITLIST_FILE.write_text("[]")

# Cap the waitlist file at 50K entries to keep reads sane. Older
# entries get rotated out (caller-visible via the 50K-1 entry being
# the oldest kept). This is a generous cap for a waitlist.
_WAITLIST_MAX = 50000

# RFC 5322 is overkill for a waitlist; this regex matches "x@y.z" with
# at least one dot in the domain. Good enough to catch typos and
# obvious garbage without rejecting valid edge cases.
_WAITLIST_RE = _re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _read_waitlist() -> list:
    try:
        return json.loads(WAITLIST_FILE.read_text() or "[]")
    except (json.JSONDecodeError, OSError):
        return []


def _write_waitlist(entries: list) -> None:
    # Keep only the most recent _WAITLIST_MAX entries (oldest dropped).
    if len(entries) > _WAITLIST_MAX:
        entries = entries[-_WAITLIST_MAX:]
    tmp = WAITLIST_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entries))
    os.replace(tmp, WAITLIST_FILE)


@app.route("/api/waitlist", methods=["POST"])
@rate_limited
def api_waitlist():
    """Public waitlist signup. Stores email + source in a flat JSON file.
    No auth required. No CSRF needed: the endpoint is POST + JSON only,
    and the side-effect is a single append to a local file.

    Returns:
        201 {email, position, total_signups} on success
        400 {error} on bad email
        200 {email, already_signed_up: true} on duplicate (idempotent)
    """
    data = request.json or {}
    email = (data.get("email") or "").strip().lower()
    if not email or len(email) > 320 or not _WAITLIST_RE.match(email):
        return jsonify({"error": "Invalid email"}), 400
    # Cap length on source to keep the file from bloating with junk.
    source = (data.get("source") or "unknown")[:64]

    with _request_log_lock:  # reuse the rate-limiter lock for FS safety
        entries = _read_waitlist()
        # Idempotent: if email already present, return 200 with
        # already_signed_up=true. Don't add a duplicate row.
        for e in entries:
            if e.get("email") == email:
                return jsonify({
                    "email": email,
                    "already_signed_up": True,
                    "position": entries.index(e) + 1,
                    "total_signups": len(entries),
                })
        entries.append({
            "email": email,
            "source": source,
            "ts": now_str(),
        })
        _write_waitlist(entries)
        return jsonify({
            "email": email,
            "position": len(entries),
            "total_signups": len(entries),
        }), 201


# ── Templates (WS-3) ────────────────────────────────────────────────
# Curated presets that map to common CPG/DTC ad placements: Amazon main
# image, Instagram 4:5, Pinterest 9:16, email header, etc. The
# operator can edit templates.json (and the running container picks up
# changes on restart) without a code deploy. Each template is a
# self-contained (prompt, preset, aspect, tier, category) tuple.
#
# Storage: /app/data/templates.json (DATA_DIR). Ships with a default
# file at scripts/templates.json that gets seeded on first boot if
# the user-editable file doesn't exist.
import json as _json_templates

_TEMPLATES_DEFAULT_PATH = SCRIPT_DIR / "templates.json"
_TEMPLATES_USER_PATH = DATA_DIR / "templates.json"

# Seed the user-editable file from the default if it doesn't exist
if not _TEMPLATES_USER_PATH.exists() and _TEMPLATES_DEFAULT_PATH.exists():
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _TEMPLATES_USER_PATH.write_text(_TEMPLATES_DEFAULT_PATH.read_text())
    except OSError:
        pass


def _read_templates() -> list:
    """Read templates from the user-editable file. Falls back to
    the default if the user file is missing/corrupt. Returns a
    list of dicts (always the .templates array, never the wrapper)."""
    for path in (_TEMPLATES_USER_PATH, _TEMPLATES_DEFAULT_PATH):
        if not path.exists():
            continue
        try:
            data = _json_templates.loads(path.read_text())
            t = data.get("templates", [])
            if isinstance(t, list) and t:
                return t
        except (OSError, _json_templates.JSONDecodeError, ValueError):
            continue
    return []


@app.route("/api/templates", methods=["GET"])
@rate_limited
def api_templates():
    """Return the curated template list. Public — no auth. The
    frontend fetches this on boot to populate the Templates panel.
    Templates are a discovery surface, not a security boundary.
    """
    return jsonify({"templates": _read_templates(), "version": 1})


# ── Whoami / BYOK status (existing) ────────────────────────────────


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


# ── Admin auth + admin pages ────────────────────────────────────────────
# Cheap shared-secret auth for the operator-only admin surface. NOT
# security-grade — anyone with the secret can read the waitlist. Fine
# for a single-operator side project, replace with real auth before
# adding more operators.
import hmac as _hmac

ADMIN_SECRET = os.environ.get("PHOTOGEN_ADMIN_SECRET", "").strip()


def _admin_authed() -> bool:
    """Return True if the request's X-Admin-Secret header matches the
    PHOTOGEN_ADMIN_SECRET env var. If the env var is empty, the admin
    surface is closed (every request 401s) — fail-closed beats fail-open."""
    if not ADMIN_SECRET:
        return False
    sent = request.headers.get("X-Admin-Secret", "")
    if not sent:
        return False
    return _hmac.compare_digest(sent, ADMIN_SECRET)


@app.route("/admin/waitlist")
def admin_waitlist():
    """Operator-only view of the waitlist JSON. Show a tiny HTML table.
    Auth: X-Admin-Secret header matching PHOTOGEN_ADMIN_SECRET env var.
    If the env var isn't set, this route always 401s."""
    if not _admin_authed():
        return (
            "<h1>401</h1><p>Set PHOTOGEN_ADMIN_SECRET and pass it as "
            "X-Admin-Secret to see the waitlist.</p>",
            401,
            {"Content-Type": "text/html; charset=utf-8"},
        )
    entries = _read_waitlist()
    total = len(entries)
    sources = {}
    for e in entries:
        s = e.get("source", "unknown")
        sources[s] = sources.get(s, 0) + 1
    rows = "\n".join(
        "<tr><td>{ts}</td><td><code>{email}</code></td><td>{src}</td></tr>".format(
            ts=e.get("ts", "?"), email=e.get("email", "?"),
            src=e.get("source", "?")
        )
        for e in entries
    )
    # Sources summary
    src_rows = "\n".join(
        "<tr><td>{s}</td><td>{n}</td></tr>".format(s=s, n=n)
        for s, n in sorted(sources.items(), key=lambda x: -x[1])
    )
    # Tiny inline HTML, no template dependency. This is an operator-only
    # page so we're not worried about its visual design.
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Photogen — Waitlist</title>
<style>
body {{ font: 14px/1.5 -apple-system, sans-serif; margin: 24px; color: #1a1a1a; background: #fafafa; }}
h1 {{ margin: 0 0 8px; font-size: 22px; }}
h2 {{ margin: 24px 0 8px; font-size: 16px; color: #444; }}
table {{ border-collapse: collapse; width: 100%; max-width: 1100px; background: #fff; }}
th, td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid #eee; }}
th {{ background: #f5f5f5; font-size: 12px; text-transform: uppercase; color: #555; }}
code {{ font: 13px ui-monospace, SF Mono, Menlo, monospace; }}
.meta {{ color: #666; font-size: 13px; margin-bottom: 16px; }}
.summary {{ display: inline-block; margin-right: 32px; }}
</style></head>
<body>
<h1>Photogen Waitlist</h1>
<p class="meta">
  <span class="summary"><b>{total}</b> total signups</span>
  <a href="/admin/waitlist.csv?ts={int(time.time())}">Download CSV</a>
</p>
<h2>By source</h2>
<table><tr><th>Source</th><th>Signups</th></tr>{src_rows}</table>
<h2>All signups (newest first)</h2>
<table>
  <tr><th>Timestamp</th><th>Email</th><th>Source</th></tr>
  {rows or '<tr><td colspan="3"><i>No signups yet.</i></td></tr>'}
</table>
</body></html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/admin/waitlist.csv")
def admin_waitlist_csv():
    """Same auth as /admin/waitlist but returns CSV for grep / Excel /
    import-into-Mailchimp workflows. Newest first."""
    if not _admin_authed():
        return "401", 401, {"Content-Type": "text/plain"}
    entries = _read_waitlist()
    import csv as _csv
    import io as _io
    buf = _io.StringIO()
    w = _csv.writer(buf, lineterminator="\n")
    w.writerow(["timestamp", "email", "source"])
    for e in entries:
        w.writerow([e.get("ts", ""), e.get("email", ""), e.get("source", "")])
    return buf.getvalue(), 200, {
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": 'attachment; filename="photogen-waitlist.csv"',
    }


@app.route("/status")
def status_page():
    """Live status page showing cost and active-job health."""
    with _jobs_lock:
        jobs_snapshot = {identifier: dict(job) for identifier, job in _jobs.items()}
    return _render_status(load_costs(), jobs_snapshot)


@app.route("/docs")
def docs_page():
    """Current authentication and endpoint reference."""
    return _render_docs()


@app.route("/privacy")
def privacy_page():
    """Static privacy policy page. We collect as little as possible;
    see templates/privacy.html for the full text. Public — no auth."""
    return render_template("privacy.html")


# ── Auth Routes (WS-2) ────────────────────────────────────────────────


@app.route("/signup", methods=["GET", "POST"])
def signup_page():
    """Render the signup form. On POST, create a magic link token and
    return it IN THE RESPONSE (no email sender yet — WS-2 doesn't ship
    with SMTP; the operator can copy the token from the server or we
    add Resend in a follow-up)."""
    if request.method == "POST":
        data = request.json or {}
        email = (data.get("email") or "").strip().lower()
        if not email or "@" not in email or "." not in email.split("@")[-1]:
            return jsonify({"error": "Invalid email"}), 400
        token = _create_magic_link_token(email)
        # For now: return the token so the operator/developer can
        # manually paste it into the login flow. When Resend free
        # tier ships, this changes to send it via email.
        return jsonify({"token": token, "email": email})
    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login_page():
    """Render the login form. On POST (magic link token), create or
    find the user and return a session token."""
    if request.method == "POST":
        data = request.json or {}
        token = (data.get("token") or "").strip()
        if not token:
            return jsonify({"error": "Token required"}), 400
        session = _consume_magic_link(token)
        if not session:
            return jsonify({"error": "Invalid or expired token"}), 400
        return jsonify({
            "session_token": session["id"],
            "email": session["email"],
            "credits_remaining": session["credits_remaining"],
            "expires_at": session["expires_at"],
        })
    return render_template("login.html")


@app.route("/api/me", methods=["GET"])
def api_me():
    """Return the current user + account status, or 401 if not
    signed in. This is the 'who am I' endpoint that the frontend
    polls on boot to decide whether to show the key input or the
    'you have N credits' badge."""
    sess = _current_session()
    if not sess:
        return jsonify({"error": "Not signed in"}), 401
    with _auth_db() as db:
        user = db.execute("SELECT * FROM users WHERE id = ?", (sess["user_id"],)).fetchone()
    if not user:
        return jsonify({"error": "User not found"}), 401
    return jsonify({
        "email": user["email"],
        "credits_remaining": user["credits_remaining"] or 0,
        "credits_used_today": user["credits_used_today"] or 0,
        "created_at": user["created_at"],
        "subscription_tier": user["subscription_tier"],
        "subscription_status": user["subscription_status"],
        "subscription_renews_at": user["subscription_renews_at"],
    })


# ── Billing (WS-6) — Stripe Checkout + webhook + credit ledger ──────────
# Three plans: Starter $19 (100 credits/mo), Pro $49 (500/mo),
# Studio $99 (1500/mo). The credit ledger is a single column on
# the users row (credits_remaining) that resets each billing
# cycle — we don't track per-cycle usage history for v1.
#
# The whole billing surface is "fail-closed when unconfigured": if
# STRIPE_SECRET_KEY isn't set, the checkout / portal / webhook
# endpoints return 503 with a clear "billing not configured" error.
# This way, the rest of the app keeps working in BYOK mode without
# Stripe ever being set up.
import stripe as _stripe_lib  # noqa: E402  (after the import block above)

# Plan catalog. Price IDs are looked up from env so the operator
# can wire to the price IDs from their Stripe dashboard without
# a code deploy. The tier label and credit count are config here.
_BILLING_PLANS = {
    "starter": {
        "label": "Starter",
        "monthly_credits": 100,
        "price_id_env": "STRIPE_PRICE_STARTER",
        "default_price": "$19/mo",  # shown if env var unset
    },
    "pro": {
        "label": "Pro",
        "monthly_credits": 500,
        "price_id_env": "STRIPE_PRICE_PRO",
        "default_price": "$49/mo",
    },
    "studio": {
        "label": "Studio",
        "monthly_credits": 1500,
        "price_id_env": "STRIPE_PRICE_STUDIO",
        "default_price": "$99/mo",
    },
}

_BILLING_PORTAL_RETURN_URL = os.environ.get(
    "PHOTOGEN_BILLING_RETURN_URL", "https://photogen.ashbi.ca/settings/billing")


def _stripe_configured() -> bool:
    """Return True if STRIPE_SECRET_KEY is set. Used as a fail-closed
    gate so the rest of the app keeps working without Stripe."""
    return bool(os.environ.get("STRIPE_SECRET_KEY", "").strip())


def _stripe_api():
    """Return a configured stripe module. Raises RuntimeError if
    Stripe is not configured — callers should call _stripe_configured
    first and return 503 if false."""
    if not _stripe_configured():
        raise RuntimeError("Stripe not configured (STRIPE_SECRET_KEY not set)")
    _stripe_lib.api_key = os.environ["STRIPE_SECRET_KEY"]
    return _stripe_lib


def _get_or_create_stripe_customer(user: dict) -> str:
    """Find the user's Stripe customer id, creating one if needed.
    Stores the id on the user row so we don't re-lookup on every
    request."""
    if user.get("stripe_customer_id"):
        return user["stripe_customer_id"]
    stripe = _stripe_api()
    customer = stripe.Customer.create(
        email=user["email"],
        metadata={"photogen_user_id": user["id"]},
    )
    cid = customer["id"]
    with _auth_db() as db:
        db.execute(
            "UPDATE users SET stripe_customer_id = ? WHERE id = ?",
            (cid, user["id"]))
        db.commit()
    return cid


def _resolve_price_id(plan: str) -> str:
    """Look up the Stripe price id for a plan from the env. Raises
    ValueError if the plan is unknown or the price id isn't set."""
    if plan not in _BILLING_PLANS:
        raise ValueError(f"Unknown plan: {plan!r}")
    price_id = os.environ.get(_BILLING_PLANS[plan]["price_id_env"], "").strip()
    if not price_id:
        raise RuntimeError(
            f"Plan {plan!r} not configured: set "
            f"{_BILLING_PLANS[plan]['price_id_env']} in /root/.env.photogen"
        )
    return price_id


@app.route("/api/billing/plans", methods=["GET"])
@rate_limited
def api_billing_plans():
    """Public list of available plans and their credit counts.
    Pricing shown as a placeholder string if the env var is unset
    (the operator can configure prices without a code deploy)."""
    out = []
    for plan_id, plan in _BILLING_PLANS.items():
        price_env = plan["price_id_env"]
        price_id = os.environ.get(price_env, "").strip()
        out.append({
            "id": plan_id,
            "label": plan["label"],
            "monthly_credits": plan["monthly_credits"],
            "price_id_configured": bool(price_id),
            "price_display": plan["default_price"] if not price_id else "configured",
        })
    return jsonify({"plans": out, "stripe_configured": _stripe_configured()})


@app.route("/api/billing/checkout", methods=["POST"])
@rate_limited
def api_billing_checkout():
    """Create a Stripe Checkout session for a plan and return the
    URL the browser should redirect to. Body: {plan: 'starter'|'pro'|'studio'}.

    Signed-in user only. Returns 503 if Stripe is not configured
    on the host (with a clear message)."""
    sess = _current_session()
    if not sess:
        return jsonify({"error": "Sign in required"}), 401
    if not _stripe_configured():
        return jsonify({
            "error": "Billing not configured",
            "message": "Set STRIPE_SECRET_KEY on the host to enable paid plans.",
        }), 503
    data = request.json or {}
    plan = (data.get("plan") or "").strip()
    if plan not in _BILLING_PLANS:
        return jsonify({
            "error": "Invalid plan",
            "valid_plans": list(_BILLING_PLANS.keys()),
        }), 400
    try:
        price_id = _resolve_price_id(plan)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    with _auth_db() as db:
        user = db.execute("SELECT * FROM users WHERE id = ?",
                          (sess["user_id"],)).fetchone()
    if not user:
        return jsonify({"error": "User not found"}), 401
    # Convert sqlite3.Row to dict so the rest of the helpers (which
    # are typed as dict) work without .get() errors.
    user = dict(user)
    try:
        customer_id = _get_or_create_stripe_customer(user)
        stripe = _stripe_api()
        session = stripe.checkout.Session.create(
            mode="subscription",
            customer=customer_id,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=_BILLING_PORTAL_RETURN_URL + "?checkout=success",
            cancel_url=_BILLING_PORTAL_RETURN_URL + "?checkout=canceled",
            metadata={"photogen_user_id": user["id"], "plan": plan},
        )
    except Exception as e:
        return jsonify({"error": "Checkout creation failed", "message": str(e)}), 500
    return jsonify({
        "url": session["url"],
        "session_id": session["id"],
        "plan": plan,
    })


@app.route("/api/billing/portal", methods=["POST"])
@rate_limited
def api_billing_portal():
    """Return a Stripe Customer Portal URL for managing the
    subscription (cancel, change card, view invoices)."""
    sess = _current_session()
    if not sess:
        return jsonify({"error": "Sign in required"}), 401
    if not _stripe_configured():
        return jsonify({"error": "Billing not configured"}), 503
    with _auth_db() as db:
        user = db.execute("SELECT * FROM users WHERE id = ?",
                          (sess["user_id"],)).fetchone()
    if not user or not user["stripe_customer_id"]:
        return jsonify({"error": "No billing account yet"}), 400
    try:
        stripe = _stripe_api()
        session = stripe.billing_portal.Session.create(
            customer=user["stripe_customer_id"],
            return_url=_BILLING_PORTAL_RETURN_URL,
        )
    except Exception as e:
        return jsonify({"error": "Portal creation failed", "message": str(e)}), 500
    return jsonify({"url": session["url"]})


@app.route("/api/billing/webhook", methods=["POST"])
def api_billing_webhook():
    """Stripe webhook receiver. Verifies the signature, then handles
    the events we care about: checkout.session.completed,
    customer.subscription.{created,updated,deleted},
    invoice.payment_{succeeded,failed}.

    No auth required — auth is via the Stripe signature header
    instead (STRIPE_WEBHOOK_SECRET env var)."""
    if not _stripe_configured():
        return "Stripe not configured", 503
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
    if not webhook_secret:
        return "Webhook secret not configured", 503
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")
    try:
        stripe = _stripe_api()
        event = stripe.Webhook.construct_event(
            payload, sig_header, webhook_secret)
    except Exception as e:
        return f"Invalid signature: {e}", 400

    etype = event.get("type", "")
    data = event.get("data", {}).get("object", {})
    user_id = (data.get("metadata") or {}).get("photogen_user_id") or None

    # Map subscription status strings to ours
    if etype == "checkout.session.completed":
        # User just paid; record subscription
        sub_id = data.get("subscription")
        customer_id = data.get("customer")
        # Resolve user from customer_id (more reliable than metadata)
        if not user_id and customer_id:
            with _auth_db() as db:
                row = db.execute(
                    "SELECT id FROM users WHERE stripe_customer_id = ?",
                    (customer_id,)).fetchone()
                if row:
                    user_id = row["id"]
        if user_id and sub_id:
            try:
                sub = stripe.Subscription.retrieve(sub_id)
                _record_subscription(user_id, sub)
            except Exception:
                # Will be updated by the subscription.* event anyway
                pass
    elif etype in ("customer.subscription.created",
                   "customer.subscription.updated"):
        if user_id:
            _record_subscription(user_id, data)
    elif etype == "customer.subscription.deleted":
        if user_id:
            _cancel_subscription(user_id)
    elif etype == "invoice.payment_succeeded":
        # New billing cycle: reset credits_used_today, top up
        # credits_remaining to the plan's monthly_credits. The plan
        # comes from the subscription's price → tier mapping.
        if user_id:
            _top_up_credits(user_id)
    elif etype == "invoice.payment_failed":
        # Mark the user as past_due but don't take credits away yet.
        # Stripe will retry. After enough failures the subscription
        # is canceled automatically.
        if user_id:
            with _auth_db() as db:
                db.execute(
                    "UPDATE users SET subscription_status = 'past_due' WHERE id = ?",
                    (user_id,))
                db.commit()
    return "", 200


def _tier_from_price_id(price_id: str) -> str | None:
    """Map a Stripe price id back to our tier slug. Returns None if
    the price id doesn't match any configured plan."""
    for plan_id, plan in _BILLING_PLANS.items():
        env_price = os.environ.get(plan["price_id_env"], "").strip()
        if env_price and env_price == price_id:
            return plan_id
    return None


def _record_subscription(user_id: str, sub: dict) -> None:
    """Update the user's subscription state from a Stripe
    subscription object. Adds the monthly credits the first time
    the user upgrades (sub.status becomes 'active' or 'trialing')."""
    status = sub.get("status", "active")
    price_id = (sub.get("items", {}).get("data") or [{}])[0].get("price", {}).get("id")
    tier = _tier_from_price_id(price_id) if price_id else None
    renews_at = (sub.get("current_period_end")
                 if sub.get("current_period_end") is not None else None)
    renews_iso = (datetime.fromtimestamp(renews_at).strftime("%Y-%m-%d %H:%M:%S")
                  if renews_at else None)
    with _auth_db() as db:
        # If the user just upgraded, top up credits. We always
        # add the monthly_credits on subscription created/updated
        # to "active"/"trialing"; for renewals the invoice
        # event handles the top-up.
        previous = db.execute(
            "SELECT subscription_tier, credits_remaining FROM users WHERE id = ?",
            (user_id,)).fetchone()
        previous_dict = dict(previous) if previous else {}
        is_new_active = (status in ("active", "trialing")
                         and not previous_dict.get("subscription_tier"))
        if is_new_active and tier:
            plan = _BILLING_PLANS[tier]
            db.execute(
                """UPDATE users SET stripe_subscription_id = ?,
                                      subscription_tier = ?,
                                      subscription_status = ?,
                                      subscription_renews_at = ?,
                                      credits_remaining = ?
                   WHERE id = ?""",
                (sub.get("id"), tier, status, renews_iso,
                 plan["monthly_credits"], user_id))
        else:
            db.execute(
                """UPDATE users SET stripe_subscription_id = ?,
                                      subscription_tier = ?,
                                      subscription_status = ?,
                                      subscription_renews_at = ?
                   WHERE id = ?""",
                (sub.get("id"), tier, status, renews_iso, user_id))
        db.commit()


def _cancel_subscription(user_id: str) -> None:
    """Subscription deleted (Stripe sends customer.subscription.deleted
    when a user cancels + their period ends). Set tier to NULL,
    status to canceled, leave credits_remaining as-is so the
    user can keep using whatever they have until the period ends."""
    with _auth_db() as db:
        db.execute(
            """UPDATE users SET subscription_tier = NULL,
                                  subscription_status = 'canceled'
               WHERE id = ?""", (user_id,))
        db.commit()


def _top_up_credits(user_id: str) -> None:
    """Called on invoice.payment_succeeded. Resets credits_used_today
    and tops up credits_remaining to the plan's monthly_credits.
    We do an absolute set rather than a +delta so re-running the
    same webhook doesn't double-credit."""
    with _auth_db() as db:
        user = db.execute(
            "SELECT subscription_tier FROM users WHERE id = ?",
            (user_id,)).fetchone()
        if not user or not user["subscription_tier"]:
            return
        plan = _BILLING_PLANS.get(user["subscription_tier"])
        if not plan:
            return
        db.execute(
            """UPDATE users SET credits_remaining = ?,
                                  credits_used_today = 0,
                                  subscription_status = 'active'
               WHERE id = ?""",
            (plan["monthly_credits"], user_id))
        db.commit()


@app.route("/settings/billing", methods=["GET"])
def settings_billing():
    """Public page that shows the current user's plan + credit
    balance + upgrade options. Redirects to /login if not signed
    in (the frontend JS reads X-Session-Token from localStorage)."""
    return render_template("billing.html")


# ── Project routes (WS-4) ────────────────────────────────────────────
# All routes here require a signed-in session. A user can only
# see/operate on their own projects (filtered by user_id at the
# SQL layer). Anonymous /api/projects/* requests return 401.

@app.route("/api/projects", methods=["POST"])
@rate_limited
def api_projects_create():
    """Create a new project. Body: {name?, source_session_id?}.
    Returns the new project (with empty generations)."""
    sess = _current_session()
    if not sess:
        return jsonify({"error": "Sign in required"}), 401
    data = request.json or {}
    name = (data.get("name") or "Untitled project").strip()[:_PROJECT_NAME_MAX]
    source_session_id = (data.get("source_session_id") or "").strip()[:64] or None
    proj = _create_project(sess["user_id"], name, source_session_id)
    return jsonify(proj), 201


@app.route("/api/projects", methods=["GET"])
@rate_limited
def api_projects_list():
    """List the signed-in user's projects, newest first.
    Skips generations on the list view (faster) — use
    /api/projects/<id> to get full data."""
    sess = _current_session()
    if not sess:
        return jsonify({"error": "Sign in required"}), 401
    return jsonify({"projects": _list_projects_for_user(sess["user_id"])})


@app.route("/api/projects/<project_id>", methods=["GET"])
@rate_limited
def api_projects_get(project_id):
    """Get a single project. Returns 404 if the project doesn't
    exist OR if the user doesn't own it (don't leak existence)."""
    sess = _current_session()
    if not sess:
        return jsonify({"error": "Sign in required"}), 401
    proj = _get_project_for_user(project_id, sess["user_id"])
    if not proj:
        return jsonify({"error": "Not found"}), 404
    return jsonify(proj)


@app.route("/api/projects/<project_id>", methods=["DELETE"])
@rate_limited
def api_projects_delete(project_id):
    """Delete a project. Idempotent: returns 200 even if already gone."""
    sess = _current_session()
    if not sess:
        return jsonify({"error": "Sign in required"}), 401
    ok = _delete_project(project_id, sess["user_id"])
    return jsonify({"deleted": ok})


@app.route("/api/projects/<project_id>/generations", methods=["POST"])
@rate_limited
def api_projects_add_generation(project_id):
    """Append a generation to a project. Body: {url, prompt, cost?, model?, ratio?}.
    Called by the editor after a successful /api/generate response
    to record the image against the active project."""
    sess = _current_session()
    if not sess:
        return jsonify({"error": "Sign in required"}), 401
    data = request.json or {}
    url = (data.get("url") or "").strip()[:_GENERATION_URL_MAX]
    if not url:
        return jsonify({"error": "url required"}), 400
    proj = _add_generation_to_project(
        project_id, sess["user_id"],
        url=url,
        prompt=(data.get("prompt") or "").strip()[:_GENERATION_PROMPT_MAX],
        cost=float(data.get("cost") or 0),
        model=str(data.get("model") or "")[:120],
        ratio=str(data.get("ratio") or "")[:16],
    )
    if not proj:
        return jsonify({"error": "Not found"}), 404
    return jsonify(proj)


@app.route("/api/projects/<project_id>/export", methods=["GET"])
@rate_limited
def api_projects_export(project_id):
    """Export a project as a zip with a JSON manifest + the image files.

    Zip structure:
      manifest.json     — {id, name, hero_url, created_at, updated_at, generations}
      images/<n>.<ext>   — the generated image, downloaded server-side
                          from the local file system. (Skipped for
                          external URLs that the server can't reach —
                          we still record the URL in the manifest.)
    The manifest is canonical — the image filenames match the
    'url' field in each generation. If the URL is local (/image/...)
    we resolve it under OUTPUT_DIR and add the file. Otherwise the
    image is referenced by URL only.
    """
    sess = _current_session()
    if not sess:
        return jsonify({"error": "Sign in required"}), 401
    proj = _get_project_for_user(project_id, sess["user_id"])
    if not proj:
        return jsonify({"error": "Not found"}), 404

    buf = _io.BytesIO()
    with _zipfile.ZipFile(buf, "w", _zipfile.ZIP_DEFLATED) as zf:
        # Manifest
        zf.writestr("manifest.json",
                    _json_projects.dumps({
                        "id": proj["id"],
                        "name": proj["name"],
                        "hero_url": proj["hero_url"],
                        "created_at": proj["created_at"],
                        "updated_at": proj["updated_at"],
                        "generations": proj["generations"],
                    }, indent=2))
        # Embed local images
        for idx, gen in enumerate(proj["generations"], start=1):
            url = gen.get("url") or ""
            if url.startswith("/image/"):
                # local file. Serve_image does the same
                # path-validity check; reuse it.
                safe_rel = _safe_output_relpath(url[len("/image/"):])
                if safe_rel is not None:
                    full = OUTPUT_DIR / safe_rel
                    if full.is_file():
                        arcname = f"images/{idx}_{safe_rel.name}"
                        zf.write(str(full), arcname)
            elif url.startswith("http://") or url.startswith("https://"):
                # External URL: don't try to fetch it (could be slow or
                # blocked). Just record the URL in the manifest (which
                # we already did above). The user can download manually.
                pass

    data = buf.getvalue()
    safe_name = (proj["name"] or "project").strip().replace(" ", "_")
    safe_name = "".join(c for c in safe_name if c.isalnum() or c in "_-")
    filename = f"photogen-{safe_name[:50]}-{proj['id'][:8]}.zip"
    return data, 200, {
        "Content-Type": "application/zip",
        "Content-Disposition": f'attachment; filename="{filename}"',
    }


# ── Library (WS-7) — every generation the user has ever made ────────────
# Scans OUTPUT_DIR for all PNG/JPG files. Each generation is a
# {path, url, name, mtime, size} entry. Filterable by prompt text
# (best-effort, since prompt is only in the JSON manifest next to
# the PNG, not the filename), aspect ratio (parsed from the
# filename), and date range.
#
# For v1 we just enumerate and paginate. The frontend's
# "Library" tab uses this to show all generations the user
# has ever made, search/filter, and click to re-load.
_LIBRARY_MAX_RESULTS = 200
_LIBRARY_PAGE_SIZE = 60


def _scan_output_dir() -> list:
    """Walk OUTPUT_DIR and return a list of {path, name, mtime, size}
    for every PNG/JPG. Sorted newest first. Cheap (no metadata
    parsing). The manifest sidecar (if present) gets included
    in a separate pass for the prompt text."""
    if not OUTPUT_DIR.is_dir():
        return []
    out = []
    for f in OUTPUT_DIR.rglob("*.png"):
        try:
            st = f.stat()
        except OSError:
            continue
        out.append({
            "path": str(f.relative_to(OUTPUT_DIR)),
            "name": f.name,
            "mtime": int(st.st_mtime),
            "size": st.st_size,
        })
    for f in OUTPUT_DIR.rglob("*.jpg"):
        try:
            st = f.stat()
        except OSError:
            continue
        out.append({
            "path": str(f.relative_to(OUTPUT_DIR)),
            "name": f.name,
            "mtime": int(st.st_mtime),
            "size": st.st_size,
        })
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def _load_prompt_for(path: str) -> str:
    """Look for a sidecar JSON file with the same stem as the
    image. The generation pipeline writes a `{stem}.json` next
    to each PNG with the prompt. Returns empty string if no
    sidecar found."""
    p = OUTPUT_DIR / path
    sidecar = p.with_suffix(".json")
    if not sidecar.is_file():
        # Try other naming patterns
        for suffix in (".json", ".prompt", ".txt"):
            cand = p.parent / (p.stem + suffix)
            if cand.is_file():
                sidecar = cand
                break
        else:
            return ""
    try:
        import json as _json
        data = _json.loads(sidecar.read_text())
        if isinstance(data, dict):
            for key in ("prompt", "user_prompt", "original_prompt"):
                if data.get(key):
                    return str(data[key])
    except (OSError, ValueError):
        return ""
    return ""


@app.route("/api/library", methods=["GET"])
@rate_limited
def api_library():
    """Return a paginated list of every generation the user has
    ever made. Signed-in only — anonymous requests get 401.

    Query params:
      - search (substring match against the prompt)
      - aspect (substring match against the filename, e.g. "1_1" or "4_5")
      - limit (default 60, max 200)
      - offset (default 0)

    Response:
      {items: [{path, url, prompt, name, mtime, size, aspect}],
       total: <int>, limit: <int>, offset: <int>}

    The aspect ratio is parsed from the filename (e.g. 2026-06-15/2026-06-15_140523_4_5.png → 4:5).
    """
    sess = _current_session()
    if not sess:
        return jsonify({"error": "Sign in required"}), 401
    search = (request.args.get("search") or "").strip().lower()
    aspect_q = (request.args.get("aspect") or "").strip()
    try:
        limit = min(int(request.args.get("limit", _LIBRARY_PAGE_SIZE)),
                    _LIBRARY_MAX_RESULTS)
    except (TypeError, ValueError):
        limit = _LIBRARY_PAGE_SIZE
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    # 4-5 → 4_5 for filename matching
    aspect_filename = aspect_q.replace(":", "_") if aspect_q else ""

    all_items = _scan_output_dir()
    # Filter
    if search or aspect_filename:
        filtered = []
        for item in all_items:
            if aspect_filename and aspect_filename not in item["path"]:
                continue
            if search:
                # Need the prompt for substring match
                prompt = _load_prompt_for(item["path"])
                if not search in prompt.lower():
                    # Also try the path itself
                    if not search in item["name"].lower():
                        continue
            filtered.append(item)
        all_items = filtered
    total = len(all_items)
    page = all_items[offset:offset + limit]

    # Build the response
    out = []
    for item in page:
        # Aspect ratio from filename: "..._4_5.png" or "..._1_1.png" or "..._16_9.png"
        aspect = ""
        name_no_ext = item["name"].rsplit(".", 1)[0]
        # Find the LAST _<digit>_<digit> pattern in the filename
        import re as _re_lib
        m = _re_lib.search(r"_(\d+)_(\d+)(?!_\d)", name_no_ext)
        if m:
            aspect = f"{m.group(1)}:{m.group(2)}"
        url = "/image/" + item["path"]
        prompt = _load_prompt_for(item["path"])
        out.append({
            "path": item["path"],
            "url": url,
            "prompt": prompt,
            "name": item["name"],
            "mtime": item["mtime"],
            "size": item["size"],
            "aspect": aspect,
        })
    return jsonify({
        "items": out,
        "total": total,
        "limit": limit,
        "offset": offset,
    })


@app.route("/api/library/<path:subpath>/delete", methods=["POST"])
@rate_limited
def api_library_delete(subpath):
    """Delete a single file from the output dir. Used by the
    'Remove from library' button. Returns 404 if the file
    doesn't exist, 200 with {deleted: bool} otherwise.

    NOTE: this is intentionally simple — it doesn't check user
    ownership of the file (the output dir is per-host, all
    signed-in users share it in this v1). When WS-8 lands with
    per-user storage, this becomes a user-ownership check.
    """
    sess = _current_session()
    if not sess:
        return jsonify({"error": "Sign in required"}), 401
    # Reject traversal (400) but accept missing files (404).
    # We use a relaxed validator: split the path, refuse empty/
    # '..' components, then resolve and check the resolved path
    # is still under OUTPUT_DIR. If the file just doesn't exist,
    # that's a 404, not a 400.
    parts = [p for p in subpath.split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        return jsonify({"error": "Invalid path"}), 400
    target = OUTPUT_DIR
    for part in parts:
        target = target / part
    try:
        resolved = target.resolve()
        base = OUTPUT_DIR.resolve()
        resolved.relative_to(base)
    except (ValueError, RuntimeError):
        return jsonify({"error": "Invalid path"}), 400
    if not resolved.is_file():
        return jsonify({"error": "Not found"}), 404
    try:
        resolved.unlink()
    except OSError as e:
        return jsonify({"error": "Delete failed", "message": str(e)}), 500
    # Also delete the sidecar JSON if it exists
    sidecar = resolved.with_suffix(".json")
    if sidecar.is_file():
        try:
            sidecar.unlink()
        except OSError:
            pass
    return jsonify({"deleted": True})


# ── SEO surface (WS-5) ─────────────────────────────────────────────
# Public, unauthenticated routes for the long-game content strategy.
# Post source: content/blog/*.md files with frontmatter. The operator
# adds a new .md file and restarts the container — no code deploy.
# Each post can link to a Photogen template (template id in frontmatter)
# for the SEO-to-product funnel.
import re as _re_seo
import html as _html_seo
import xml.etree.ElementTree as _ET
from creative_studio_app.seo import (
    canonical_url as _seo_canonical_url,
    inline_markdown as _seo_inline_markdown,
    load_blog_posts as _seo_load_blog_posts,
    markdown_to_html as _seo_markdown_to_html,
    parse_blog_post as _seo_parse_blog_post,
)

BLOG_CONTENT_DIR = APP_ROOT / "content" / "blog"
BLOG_CONTENT_DIR.mkdir(parents=True, exist_ok=True)
_BLOG_FRONTMATTER_RE = _re_seo.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", _re_seo.DOTALL)


def _parse_blog_post(path: Path) -> dict:
    """Parse a single blog post .md file. Returns {slug, title, description,
    date, tags, body_md, body_html, template_id} or {} if invalid."""
    return _seo_parse_blog_post(path)


def _markdown_to_html(md: str) -> str:
    """Very small markdown → HTML converter. Just enough for blog
    posts: # h1, ## h2, ### h3, **bold**, *italic*, [text](url),
    lists (lines starting with -), fenced code blocks. NOT a full
    CommonMark implementation; the operator should keep posts simple."""
    return _seo_markdown_to_html(md)


def _inline_md(s: str) -> str:
    """Inline: **bold**, *italic*, [text](url), `code`."""
    return _seo_inline_markdown(s)


def _load_all_blog_posts() -> list:
    """Return all blog posts, newest first."""
    return _seo_load_blog_posts(BLOG_CONTENT_DIR)


def _canonical_url(path: str) -> str:
    """Build the canonical URL for a public page. Sitemap uses
    these to tell search engines about the page."""
    return _seo_canonical_url(path)


@app.route("/robots.txt")
def robots_txt():
    """Public robots.txt. Allow all + point at sitemap."""
    return (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin/\n"
        "Disallow: /api/\n"
        f"Sitemap: {_canonical_url('/sitemap.xml')}\n"
    ), 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/sitemap.xml")
def sitemap_xml():
    """Public sitemap.xml. Lists the static pages + all blog posts.
    Search engines read this to find all indexable URLs."""
    base = _canonical_url("")
    urls = [
        ("/", "weekly", "1.0"),
        ("/blog", "weekly", "0.9"),
        ("/privacy", "monthly", "0.3"),
    ]
    for post in _load_all_blog_posts():
        urls.append((f"/blog/{post['slug']}", "monthly", "0.7"))
    root = _ET.Element("urlset", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")
    for path, freq, prio in urls:
        url = _ET.SubElement(root, "url")
        _ET.SubElement(url, "loc").text = base + path
        _ET.SubElement(url, "changefreq").text = freq
        _ET.SubElement(url, "priority").text = prio
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + _ET.tostring(root, encoding="unicode")
    return xml, 200, {"Content-Type": "application/xml; charset=utf-8"}


@app.route("/blog")
def blog_index():
    """Public blog list. Renders a simple HTML page with each post's
    title, description, and date."""
    posts = _load_all_blog_posts()
    items_html = "".join(
        f'<li class="blog-list-item">'
        f'<a href="/blog/{_html_seo.escape(p["slug"])}">'
        f'{_html_seo.escape(p["title"])}</a>'
        f'<p class="blog-list-meta">{_html_seo.escape(p["date"])} &middot; '
        f'{_html_seo.escape(p["description"])}</p>'
        f'</li>'
        for p in posts
    )
    if not items_html:
        items_html = '<li class="blog-list-item"><i>No posts yet. Check back soon.</i></li>'
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Blog — Photogen</title>
<meta name="description" content="Practical guides on AI product photography, CPG/DTC creative, and shipping ad campaigns faster.">
<link rel="canonical" href="{_canonical_url('/blog')}">
<meta property="og:title" content="Photogen Blog">
<meta property="og:description" content="Practical guides on AI product photography and CPG/DTC creative.">
<meta property="og:type" content="website">
<meta property="og:url" content="{_canonical_url('/blog')}">
<link rel="stylesheet" href="/static/app.css">
<style>
body {{ max-width: 720px; margin: 40px auto; padding: 0 24px; color: var(--text, #1a1a1a); background: var(--bg, #fafafa); font: 16px/1.6 -apple-system, system-ui, sans-serif; }}
h1 {{ font-size: 32px; margin: 0 0 8px; }}
.lede {{ color: var(--text-2, #333); font-size: 18px; margin: 0 0 24px; }}
.blog-list {{ list-style: none; padding: 0; }}
.blog-list-item {{ padding: 20px 0; border-bottom: 1px solid var(--border, #eee); }}
.blog-list-item:last-child {{ border-bottom: none; }}
.blog-list-item a {{ color: var(--text, #1a1a1a); font-weight: 600; font-size: 20px; text-decoration: none; }}
.blog-list-item a:hover {{ color: var(--accent, #6c63ff); }}
.blog-list-meta {{ color: var(--text-3, #666); font-size: 14px; margin: 4px 0 0; }}
.back {{ font-size: 14px; color: var(--accent, #6c63ff); }}
</style>
</head>
<body>
<p class="back"><a href="/">&larr; Photogen</a></p>
<h1>Blog</h1>
<p class="lede">Practical guides on AI product photography, CPG/DTC creative, and shipping ad campaigns faster.</p>
<ul class="blog-list">{items_html}</ul>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/blog/<slug>")
def blog_post(slug):
    """A single blog post. Renders with the same minimal style as
    the index, plus JSON-LD structured data for SEO (Article schema
    so Google can show the post with rich snippet metadata)."""
    path = BLOG_CONTENT_DIR / f"{slug}.md"
    post = _parse_blog_post(path)
    if not post:
        return f"<h1>Not found</h1><p>No post named {slug!r}.</p>", 404, {"Content-Type": "text/html; charset=utf-8"}
    # Optional CTA: if the post has a template_id, link to the editor with
    # that template pre-loaded. This is the SEO-to-product funnel.
    cta_html = ""
    if post.get("template_id"):
        cta_html = (
            f'<p class="cta"><a class="cta-btn" href="/app?template='
            f'{_html_seo.escape(post["template_id"])}">Try this template in Photogen →</a></p>'
        )
    # JSON-LD structured data
    jsonld = jsonify({
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": post["title"],
        "description": post["description"],
        "datePublished": post["date"],
        "author": {"@type": "Organization", "name": "Photogen"},
        "publisher": {"@type": "Organization", "name": "Photogen"},
    }).get_data(as_text=True)
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_html_seo.escape(post['title'])} — Photogen Blog</title>
<meta name="description" content="{_html_seo.escape(post['description'])}">
<link rel="canonical" href="{_canonical_url('/blog/' + post['slug'])}">
<meta property="og:title" content="{_html_seo.escape(post['title'])}">
<meta property="og:description" content="{_html_seo.escape(post['description'])}">
<meta property="og:type" content="article">
<meta property="og:url" content="{_canonical_url('/blog/' + post['slug'])}">
<script type="application/ld+json">{jsonld}</script>
<link rel="stylesheet" href="/static/app.css">
<style>
body {{ max-width: 720px; margin: 40px auto; padding: 0 24px; color: var(--text, #1a1a1a); background: var(--bg, #fafafa); font: 16px/1.7 -apple-system, system-ui, sans-serif; }}
h1 {{ font-size: 32px; margin: 16px 0 8px; line-height: 1.2; }}
.meta {{ color: var(--text-3, #666); font-size: 14px; margin-bottom: 32px; }}
.post-body h1, .post-body h2 {{ margin-top: 32px; }}
.post-body p, .post-body ul {{ margin: 0 0 16px; }}
.post-body code {{ background: var(--surface, #f5f5f5); padding: 2px 6px; border-radius: 4px; font: 14px ui-monospace, monospace; }}
.back {{ font-size: 14px; color: var(--accent, #6c63ff); }}
.tags {{ margin: 32px 0; }}
.tag {{ display: inline-block; padding: 2px 10px; background: var(--surface, #f5f5f5); border-radius: 12px; font-size: 12px; color: var(--text-2, #555); margin-right: 6px; }}
.cta {{ margin: 32px 0; padding: 20px; background: var(--surface, rgba(108,99,255,.08)); border-radius: 8px; text-align: center; }}
.cta-btn {{ display: inline-block; padding: 10px 20px; background: var(--accent, #6c63ff); color: #fff; border-radius: 6px; text-decoration: none; font-weight: 600; }}
</style>
</head>
<body>
<p class="back"><a href="/blog">&larr; All posts</a></p>
<h1>{_html_seo.escape(post['title'])}</h1>
<p class="meta">{_html_seo.escape(post['date'])}</p>
<div class="post-body">{post['body_html']}</div>
{cta_html}
{"<p class='tags'>" + "".join(f'<span class="tag">{_html_seo.escape(t)}</span>' for t in post['tags']) + "</p>" if post['tags'] else ""}
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/history")
def history_page():
    """Render persistent generation history."""
    return _render_history(SESSIONS_DIR, load_json)


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
        # Generated output paths are immutable: refinements and retries create a
        # new file instead of replacing an existing image. Let browsers and CDNs
        # retain these multi-megabyte assets while preserving Flask's ETag and
        # conditional-request support.
        response = send_from_directory(
            str(resolved.parent),
            resolved.name,
            conditional=True,
            max_age=31536000,
        )
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
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
