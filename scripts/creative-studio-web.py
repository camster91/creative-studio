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
import hashlib
import smtplib
import urllib.error
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, List, Dict

from flask import Flask, request, jsonify, has_request_context

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
from creative_studio_app import auth as _auth_service
from creative_studio_app import projects as _project_service
from creative_studio_app import campaigns as _campaign_service
from creative_studio_app import campaign_delivery as _campaign_delivery
from creative_studio_app import generation as _generation_service
from creative_studio_app import delivery as _delivery_service
from creative_studio_app import iterations as _iteration_service
from creative_studio_app import chat as _chat_service
from creative_studio_app import billing as _billing_service
from creative_studio_app.uploads import save_image_upload as _persist_image_upload
from creative_studio_app.compositing import validate_foreground_file as _validate_foreground_file
from creative_studio_app.observability import install_request_metrics
from creative_studio_app.seo_routes import create_blueprint as _create_seo_blueprint
from creative_studio_app.informational_routes import (
    create_blueprint as _create_informational_blueprint,
)
from creative_studio_app.account_routes import create_blueprint as _create_account_blueprint
from creative_studio_app.billing_routes import create_blueprint as _create_billing_blueprint
from creative_studio_app.project_routes import create_blueprint as _create_project_blueprint
from creative_studio_app.campaign_routes import create_blueprint as _create_campaign_blueprint
from creative_studio_app.library_routes import create_blueprint as _create_library_blueprint
from creative_studio_app.state_routes import create_blueprint as _create_state_blueprint
from creative_studio_app.support_routes import create_blueprint as _create_support_blueprint
from creative_studio_app.chat_routes import create_blueprint as _create_chat_blueprint
from creative_studio_app.core_routes import create_blueprint as _create_core_blueprint
from creative_studio_app.delivery_routes import create_blueprint as _create_delivery_blueprint
from creative_studio_app.generation_routes import create_blueprint as _create_generation_blueprint
from creative_studio_app.iteration_routes import create_blueprint as _create_iteration_blueprint
from creative_studio_app.jobs import (
    DurableJobStore,
    evict_old_jobs as _evict_jobs,
    job_id as _new_job_id,
    run_job_background as _start_job,
)
from creative_studio_app.version_graph import VersionGraphStore
from creative_studio_app.version_routes import create_blueprint as _create_version_blueprint
from creative_studio_app.provider_ledger import ProviderLedger, correlation_id as _provider_correlation_id
from creative_studio_app.provider_metrics_routes import create_blueprint as _create_provider_metrics_blueprint
from creative_studio_app.provider_errors import error_code as _provider_error_code
from creative_studio_app.figma_oauth import FigmaOAuthClient, FigmaOAuthStore
from creative_studio_app.figma_oauth_routes import (
    create_blueprint as _create_figma_oauth_blueprint,
    owner_context as _figma_owner_context,
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


def _require_api_key(credits: int = 1) -> tuple:
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

    `credits` is how many credits the call costs a signed-in user on
    the credit path (generation callers pass images x tier weight).
    The deduction is all-or-nothing: a short balance spends nothing.
    Only call this from endpoints that actually generate; read-only
    endpoints use _require_access(), which never spends.

    Returns: (key, err, used_trial_credit) where used_trial_credit
    is True if we burned credits on this call. The third value
    is None if err is not None.
    """
    key: Optional[str] = _get_api_key()
    if key:
        return key, None, False

    # Try the credit fallback
    sess = _current_session()
    balance = sess.get("credits_remaining", 0) if sess else 0
    if sess and balance >= credits:
        # Check the server key BEFORE spending so a misconfigured host
        # never burns credits on a request that can't run.
        if not SERVER_API_KEY:
            return None, (
                jsonify({
                    "error": "No server-side API key configured for credit-based generation",
                    "message": "Set GEMINI_API_KEY on the host. No credits were charged.",
                }),
                500,
            ), None
        ok, _remaining = _spend_credits(sess["user_id"], credits)
        if ok:
            return SERVER_API_KEY, None, True
        # Lost a race with a concurrent request; report the fresh balance.
        sess = _current_session()
        balance = sess.get("credits_remaining", 0) if sess else 0
    if sess and 0 < balance < credits:
        return None, (
            jsonify({
                "error": "Not enough credits",
                "message": (
                    f"This request needs {credits} credits and you have {balance}. "
                    "Use fewer variations or a lower quality tier, or add your own Gemini API key."
                ),
                "credits_required": credits,
                "credits_remaining": balance,
            }),
            402,
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


def _require_access():
    """Non-spending auth gate for read-only / status / cancel endpoints.

    Accepts the same callers as _require_api_key() (own key, server
    fallback, or a signed-in session) but never touches credits, so
    polling a job or listing chat history is free. Returns an error
    response, or None when the caller may proceed.
    """
    if _get_api_key() or _current_session():
        return None
    return (
        jsonify({
            "error": "BYOK or sign-in required",
            "message": "Add your Gemini API key in the editor sidebar, or sign in.",
        }),
        402,
    )


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

_job_store = DurableJobStore(DATA_DIR / "jobs.db")
_job_store.recover_interrupted()
_version_store = VersionGraphStore(DATA_DIR / "versions.db")
_provider_ledger = ProviderLedger(DATA_DIR / "provider-ledger.db")

_figma_store = None
_figma_client = None
_figma_public_url = os.environ.get("PUBLIC_URL", "").rstrip("/")
_figma_oauth_configured = bool(
    os.environ.get("FIGMA_OAUTH_CLIENT_ID")
    and os.environ.get("FIGMA_OAUTH_CLIENT_SECRET")
    and os.environ.get("FIGMA_TOKEN_ENCRYPTION_KEY")
    and _figma_public_url.startswith("https://")
)
if _figma_oauth_configured:
    try:
        _figma_store = FigmaOAuthStore(
            DATA_DIR / "figma-oauth.db", os.environ["FIGMA_TOKEN_ENCRYPTION_KEY"]
        )
        _figma_client = FigmaOAuthClient(
            os.environ["FIGMA_OAUTH_CLIENT_ID"],
            os.environ["FIGMA_OAUTH_CLIENT_SECRET"],
            f"{_figma_public_url}/api/figma/oauth/callback",
        )
    except ValueError:
        _figma_oauth_configured = False

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


def _record_provider_results(
    owner_id: str,
    job_id: str,
    results,
    *,
    estimated_cost_each: float,
    latency_ms: float,
    provider: str = "google-gemini",
    model_hint: str = "unknown",
):
    """Account for each attempted provider call without creative payloads."""
    items = results if isinstance(results, list) and results else [{}]
    traces = _take_provider_traces()
    if len(traces) > len(items):
        items = list(items) + [
            {"error": "failed", "error_code": trace.get("outcome")}
            for trace in traces[len(items):]
        ]
    per_call_latency = max(0, latency_ms) / max(1, len(items))
    for index, item in enumerate(items):
        item = item if isinstance(item, dict) else {}
        trace = traces[index] if index < len(traces) else {}
        error_code = item.get("error_code")
        outcome = {
            "quota_exhausted": "quota_exhausted",
            "timeout": "timeout",
            "service_unavailable": "service_unavailable",
            "provider_failed": "provider_failed",
        }.get(
            error_code,
            trace.get("outcome") if not item.get("error") else "provider_failed",
        ) or "completed"
        call_id = (
            item.get("provider_correlation_id")
            or trace.get("correlation_id")
            or _provider_correlation_id()
        )
        item["provider_correlation_id"] = call_id
        _provider_ledger.record(
            owner_id=owner_id,
            job_id=job_id,
            provider=provider,
            model=item.get("model") or model_hint,
            estimated_cost=estimated_cost_each,
            actual_cost=item.get("cost") or 0,
            latency_ms=trace.get("latency_ms", per_call_latency),
            outcome=outcome,
            correlation_id=call_id,
        )


def session_cost(session_id: str) -> float:
    return sum(e.get("cost", 0) for e in load_session(session_id).get("entries", []))


# ─── Session management ────────────────────────────────────────────────


def new_session_id():
    return "sess_" + uuid.uuid4().hex[:8]


def session_path(session_id: str) -> Path:
    if not isinstance(session_id, str) or not re.fullmatch(r"sess_[0-9a-f]{8}", session_id):
        raise ValueError("Invalid session id")
    return SESSIONS_DIR / f"{session_id}.json"


def load_session(session_id: str) -> dict:
    return load_json(
        session_path(session_id),
        {"id": session_id, "created_at": now_str(), "entries": []},
    )


def save_session(session_id: str, data: dict):
    save_json(session_path(session_id), data)


def add_entry(session_id: str, entry: dict, owner_id: str | None = None):
    result = {"node_id": None}

    def _do():
        data = load_session(session_id)
        actor_id = owner_id or (_current_actor_id() if has_request_context() else None)
        if actor_id:
            existing = data.get("owner_id")
            if existing and existing != actor_id:
                raise PermissionError("Session belongs to another user")
            data["owner_id"] = actor_id
            if os.environ.get("CREATIVE_VERSION_GRAPH_ENABLED", "true").lower() in ("1", "true", "yes"):
                _version_store.migrate_legacy(data, actor_id)
        data["entries"].append({"time": now_str(), **entry})
        save_session(session_id, data)
        if actor_id and os.environ.get("CREATIVE_VERSION_GRAPH_ENABLED", "true").lower() in ("1", "true", "yes"):
            operation = {
                "direct": "generate",
                "generate": "generate",
                "variations": "variation",
                "refine": "refine",
                "composite": "composite",
                "sceneset": "composite",
                "chat": "chat",
            }.get(entry.get("type"), "generate")
            parent_id = entry.get("parent_node_id")
            if "parent_node_id" not in entry:
                graph = _version_store.graph(session_id, actor_id)
                parent_id = graph["current_node_id"] if graph else None
            node = _version_store.add_node(
                session_id,
                actor_id,
                operation=operation,
                asset_url=entry.get("image_url") or None,
                parent_id=parent_id,
                prompt=entry.get("prompt") or entry.get("note") or "",
                model=entry.get("model") or "",
                cost=entry.get("cost") or 0,
                status="completed" if entry.get("image_url") else "partial",
                metadata={
                    "note": str(entry.get("note") or "")[:200],
                    **({"composite_manifest": entry["composite_manifest"]}
                       if isinstance(entry.get("composite_manifest"), dict) else {}),
                },
            )
            result["node_id"] = node["id"]
    _with_json_lock(_do)
    return result["node_id"]


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


def _version_asset_exists(url: str) -> bool:
    if not isinstance(url, str) or not url.startswith("/image/"):
        return False
    return _safe_output_relpath(url[len("/image/") :]) is not None


def _validate_version_parent(session_id: str, owner_id: str, node_id: str | None) -> bool:
    return _version_store.owns_node(session_id, owner_id, node_id)


def _current_version_node(session_id: str, owner_id: str) -> str | None:
    if owner_id:
        _version_store.migrate_legacy(load_session(session_id), owner_id)
    graph = _version_store.graph(session_id, owner_id)
    return graph["current_node_id"] if graph else None


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
_provider_trace_state = threading.local()


def _tracked_provider_run(*args, **kwargs):
    """Attach a caller-safe correlation ID before the provider subprocess."""
    call_id = _provider_correlation_id()
    environment = dict(kwargs.get("env") or os.environ)
    environment["CREATIVE_PROVIDER_CORRELATION_ID"] = call_id
    kwargs["env"] = environment
    started = time.monotonic()
    outcome = "completed"
    try:
        return subprocess.run(*args, **kwargs)
    except BaseException as error:
        outcome = _provider_error_code(error)
        raise
    finally:
        traces = getattr(_provider_trace_state, "calls", None)
        if traces is None:
            traces = []
            _provider_trace_state.calls = traces
        traces.append({
            "correlation_id": call_id,
            "latency_ms": (time.monotonic() - started) * 1000,
            "outcome": outcome,
        })


def _take_provider_traces() -> list[dict]:
    traces = list(getattr(_provider_trace_state, "calls", []))
    _provider_trace_state.calls = []
    return traces

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
        run=_tracked_provider_run,
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
        run=_tracked_provider_run,
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
        run=_tracked_provider_run,
        to_image_url=image_url,
    )



def run_cli_qc(image_path: str, api_key: str) -> dict:
    try:
        estimated_cost = float(os.environ["CREATIVE_QC_ESTIMATED_COST_USD"])
    except (KeyError, ValueError):
        estimated_cost = None
    return _delivery_service.run_qc(
        image_path,
        api_key,
        launch_script=Path(__file__).parent.parent / "launch.sh",
        run=_tracked_provider_run,
        estimated_cost_usd=estimated_cost,
    )



def run_cli_refine(image_path: str, changes: str, api_key: str, tier: str) -> List[Dict]:
    return _iteration_service.refine(
        image_path,
        changes,
        api_key,
        tier,
        output_dir=OUTPUT_DIR,
        launch_script=Path(__file__).parent.parent / "launch.sh",
        run=_tracked_provider_run,
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
        run=_tracked_provider_run,
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
        run=_tracked_provider_run,
        record_cost=track_cost,
        to_image_url=image_url,
    )



# ─── Chat multi-turn state ──────────────────────────────────────────────
_chat_sessions: Dict[str, dict] = {}


def run_cli_chat_turn(
    session_key: str,
    api_key: str,
    prompt: str,
    tier: str,
    aspect: str,
    input_image: Optional[str] = None,
) -> tuple[List[Dict], dict]:
    return _chat_service.turn(
        _chat_sessions,
        session_key,
        api_key,
        prompt,
        tier,
        aspect,
        input_image=input_image,
        output_dir=OUTPUT_DIR,
        script_path=SCRIPT_PATH,
        python_executable=sys.executable,
        tier_models=_TIER_MODEL,
        run=_tracked_provider_run,
        record_cost=track_cost,
        to_image_url=image_url,
    )


def chat_session_history(session_key: str) -> List[dict]:
    return _chat_service.history(_chat_sessions, session_key)


def chat_reset(session_key: str) -> dict:
    return _chat_service.reset(_chat_sessions, session_key)



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

try:
    _metrics_logger = logging.getLogger("creative_studio.metrics")
    _metrics_logger.propagate = False
    _metrics_handler = RotatingFileHandler(
        str(DATA_DIR / "request-metrics.jsonl"),
        maxBytes=10_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    _metrics_handler.setFormatter(logging.Formatter("%(message)s"))
    _metrics_logger.addHandler(_metrics_handler)
    _metrics_logger.setLevel(logging.INFO)
    _emit_metric = _metrics_logger.info
    install_request_metrics(app, _emit_metric)
except Exception:
    _emit_metric = lambda _event: None
    install_request_metrics(app, _emit_metric)

# ── Auth and project persistence services ───────────────────────────────
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


def _current_actor_id() -> str | None:
    """Stable owner key for persistence; never stores a raw provider key."""
    session = _current_session()
    if session:
        return f"user:{session['user_id']}"
    key = request.headers.get("X-API-Key", "").strip()
    if key:
        return "key:" + hashlib.sha256(key.encode()).hexdigest()
    return None


def _owned_asset_paths(user_id: str) -> set[str]:
    if os.environ.get("CREATIVE_ALLOW_UNOWNED_ASSETS") == "1":
        return {
            str(path.relative_to(OUTPUT_DIR))
            for path in OUTPUT_DIR.rglob("*")
            if path.is_file() and path.suffix.lower().lstrip(".") in _IMAGE_EXTS
        }
    owner_id = f"user:{user_id}"
    result = set()
    for path in SESSIONS_DIR.glob("*.json"):
        data = load_json(path)
        if data.get("owner_id") != owner_id:
            continue
        for entry in data.get("entries", []):
            url = entry.get("image_url", "")
            if isinstance(url, str) and url.startswith("/image/"):
                result.add(url[len("/image/") :])
    return result


def _save_upload(upload, purpose: str) -> Path:
    owner_id = _current_actor_id()
    if not owner_id:
        raise ValueError("Sign in or provide an API key before uploading")
    destination = _persist_image_upload(
        upload,
        DATA_DIR / "uploads",
        purpose=purpose,
        owner_id=owner_id,
        max_bytes=int(os.environ.get("CREATIVE_MAX_UPLOAD_BYTES", str(16 * 1024 * 1024))),
        max_dimension=int(os.environ.get("CREATIVE_MAX_IMAGE_DIMENSION", "12000")),
        max_pixels=int(os.environ.get("CREATIVE_MAX_IMAGE_PIXELS", "40000000")),
        retention_days=int(os.environ.get(
            "CREATIVE_PACK_RETENTION_DAYS" if purpose == "campaign-pack" else "CREATIVE_UPLOAD_RETENTION_DAYS",
            "3650" if purpose == "campaign-pack" else "30",
        )),
    )
    if purpose == "campaign-pack":
        try:
            _validate_foreground_file(destination)
        except ValueError:
            destination.unlink(missing_ok=True)
            destination.with_suffix(".meta.json").unlink(missing_ok=True)
            raise
    return destination


def _deliver_magic_link(email: str, token: str) -> bool:
    global _consecutive_delivery_failures
    host = os.environ.get("SMTP_HOST", "").strip()
    sender = os.environ.get("MAGIC_LINK_FROM", "").strip()
    public_url = os.environ.get("PUBLIC_URL", "").rstrip("/")
    if not host or not sender or not public_url.startswith("https://"):
        _record_delivery_result(False, "configuration")
        return False
    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
    except ValueError:
        _record_delivery_result(False, "configuration")
        return False
    user = os.environ.get("SMTP_USERNAME", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    if bool(user) != bool(password):
        _record_delivery_result(False, "configuration")
        return False
    message = (
        f"From: {sender}\r\nTo: {email}\r\nSubject: Your Photogen sign-in link\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\n"
        f"Open this single-use sign-in link:\n\n{public_url}/login#token={token}\n"
        "\nThe link expires in 60 minutes. If you did not request it, ignore this email.\n"
    )
    try:
        with smtplib.SMTP(host, port, timeout=15) as client:
            client.starttls()
            if user:
                client.login(user, password)
            client.sendmail(sender, [email], message.encode("utf-8"))
        _record_delivery_result(True, "accepted")
        return True
    except smtplib.SMTPResponseException:
        _record_delivery_result(False, "rejected")
        return False
    except (OSError, smtplib.SMTPException, ValueError):
        _record_delivery_result(False, "unavailable")
        return False


_delivery_failure_lock = threading.Lock()
_consecutive_delivery_failures = 0


def _record_delivery_result(success: bool, outcome: str) -> None:
    """Emit privacy-safe delivery health and alert after sustained failures."""
    global _consecutive_delivery_failures
    with _delivery_failure_lock:
        _consecutive_delivery_failures = 0 if success else _consecutive_delivery_failures + 1
        failures = _consecutive_delivery_failures
    _emit_metric(json.dumps({
        "schema_version": 1,
        "event": "magic_link_delivery",
        "success": success,
        "outcome": outcome,
        "consecutive_failures": failures,
    }, separators=(",", ":"), sort_keys=True))
    try:
        threshold = max(
            1, int(os.environ.get("CREATIVE_EMAIL_FAILURE_ALERT_THRESHOLD", "5"))
        )
    except ValueError:
        threshold = 5
    if not success and failures == threshold:
        app.logger.error(
            "magic_link_delivery_sustained_failure threshold=%d", threshold
        )


def _expose_magic_link_token() -> bool:
    return os.environ.get("CREATIVE_EXPOSE_MAGIC_LINK_TOKEN") == "1"


def _use_trial_credit(user_id: str) -> tuple[bool, int]:
    return _auth_service.use_trial_credit(AUTH_DB, user_id)


def _spend_credits(user_id: str, amount: int) -> tuple[bool, int]:
    return _auth_service.spend_credits(AUTH_DB, user_id, amount)


def _refund_credits(user_id: str, amount: int) -> None:
    _auth_service.refund_credits(AUTH_DB, user_id, amount)


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


def _create_campaign(user_id: str, payload: dict) -> dict:
    return _campaign_service.create_bundle(AUTH_DB, user_id, payload)


def _list_campaigns(user_id: str) -> list:
    return _campaign_service.list_for_user(AUTH_DB, user_id)


def _list_brand_passports(user_id: str) -> list:
    return _campaign_service.list_brand_passports(AUTH_DB, user_id)


def _list_product_truth(user_id: str, brand_id: str | None = None) -> list:
    return _campaign_service.list_product_truth(AUTH_DB, user_id, brand_id)


def _get_product_truth(product_id: str, user_id: str) -> dict | None:
    return _campaign_service.get_product_truth(AUTH_DB, product_id, user_id)


def _create_product_claim(product_id: str, user_id: str, payload: dict) -> dict | None:
    return _campaign_service.create_claim(AUTH_DB, product_id, user_id, payload)


def _list_product_claims(product_id: str, user_id: str) -> list:
    return _campaign_service.list_claims(AUTH_DB, product_id, user_id)


def _retire_product_claim(claim_id: str, product_id: str, user_id: str,
                          reason: str) -> dict | None:
    return _campaign_service.retire_claim(AUTH_DB, claim_id, product_id, user_id, reason)


def _get_campaign(campaign_id: str, user_id: str) -> dict | None:
    return _campaign_service.get(AUTH_DB, campaign_id, user_id)


def _mark_campaign_started(campaign_id: str, user_id: str, session_id: str | None = None) -> None:
    _campaign_service.mark_started(AUTH_DB, campaign_id, user_id, session_id)


def _build_campaign_bundle(campaign: dict, sources: list) -> tuple[Path, dict]:
    return _campaign_delivery.build_bundle(
        campaign, sources, DATA_DIR / "campaign-bundles",
        max_bundle_bytes=int(os.environ.get("CREATIVE_MAX_CAMPAIGN_BUNDLE_BYTES", str(256 * 1024 * 1024))),
    )


def _record_campaign_bundle(campaign_id: str, user_id: str, manifest: dict,
                            bundle_path: Path, session_id: str | None = None) -> dict | None:
    root = (DATA_DIR / "campaign-bundles").resolve()
    relative = str(bundle_path.resolve().relative_to(root))
    return _campaign_service.record_bundle(
        AUTH_DB, campaign_id, user_id, manifest, relative, session_id,
    )


def _list_campaign_bundles(campaign_id: str, user_id: str) -> list:
    return _campaign_service.list_bundles(AUTH_DB, campaign_id, user_id)


def _create_campaign_exception(campaign_id: str, user_id: str, **fields) -> dict | None:
    return _campaign_service.create_exception(AUTH_DB, campaign_id, user_id, **fields)


def _list_campaign_exceptions(campaign_id: str, user_id: str) -> list:
    return _campaign_service.list_exceptions(AUTH_DB, campaign_id, user_id)


def _resolve_campaign_exception(exception_id: str, campaign_id: str, user_id: str,
                                **resolution) -> dict | None:
    return _campaign_service.resolve_exception(
        AUTH_DB, exception_id, campaign_id, user_id, **resolution,
    )


def _campaign_exception_gate(campaign_id: str, user_id: str, image_urls: list[str]) -> dict | None:
    return _campaign_service.bundle_exception_gate(AUTH_DB, campaign_id, user_id, image_urls)


def _record_campaign_qc(campaign_id: str, user_id: str, assessment: dict,
                        asset_url: str | None = None) -> list | None:
    return _campaign_service.record_qc_exceptions(
        AUTH_DB, campaign_id, user_id, assessment, asset_url,
    )


def _record_campaign_preflight(campaign_id: str, user_id: str, campaign: dict,
                               sources: list) -> list:
    recorded = []
    for finding in _campaign_delivery.preflight_findings(campaign, sources):
        item = _campaign_service.create_exception(
            AUTH_DB, campaign_id, user_id, source="channel", **finding,
        )
        if item:
            recorded.append(item)
    return recorded


def _get_campaign_bundle(bundle_id: str, campaign_id: str, user_id: str) -> dict | None:
    return _campaign_service.get_bundle(AUTH_DB, bundle_id, campaign_id, user_id)


def _campaign_bundle_path(bundle: dict) -> Path | None:
    root = (DATA_DIR / "campaign-bundles").resolve()
    try:
        path = (root / bundle["zip_relpath"]).resolve()
        path.relative_to(root)
    except (KeyError, ValueError, OSError):
        return None
    return path if path.is_file() and not path.is_symlink() else None


def _attach_campaign_pack(campaign_id: str, user_id: str, asset_name: str, sha256: str) -> dict | None:
    return _campaign_service.attach_pack_asset(AUTH_DB, campaign_id, user_id, asset_name, sha256)


def _resolve_campaign_pack(campaign_id: str, user_id: str) -> Path | None:
    stored = _campaign_service.internal_pack_asset(AUTH_DB, campaign_id, user_id)
    if not stored:
        return None
    asset_name, expected_sha256 = stored
    if not asset_name or Path(asset_name).name != asset_name:
        return None
    upload_dir = (DATA_DIR / "uploads").resolve()
    asset = (upload_dir / asset_name).resolve()
    try:
        asset.relative_to(upload_dir)
        metadata = load_json(asset.with_suffix(".meta.json"))
    except (ValueError, OSError):
        return None
    if (not asset.is_file() or asset.is_symlink()
            or metadata.get("owner_id") != f"user:{user_id}"
            or metadata.get("purpose") != "campaign-pack"
            or metadata.get("stored_name") != asset_name
            or hashlib.sha256(asset.read_bytes()).hexdigest() != expected_sha256):
        return None
    return asset



# ── Simple in-memory rate limiter ───────────────────────────────────────
_request_log: Dict[str, list] = {}
_request_log_lock = threading.Lock()
_RATE_LIMIT = max(1, int(os.environ.get("RATE_LIMIT_PER_MINUTE", "60")))

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
    """Run a job while mirroring terminal state to durable owner-scoped storage."""
    _job_store.update(job_id, status="running")

    def durable_work():
        try:
            result = fn(*args, **kwargs)
            terminal = result.pop("_job_status", "completed") if isinstance(result, dict) else "completed"
            error_code = result.pop("_job_error_code", None) if isinstance(result, dict) else None
            actual_cost = sum(item.get("cost", 0) for item in result.get("images", [])) if isinstance(result, dict) else 0
            _job_store.update(
                job_id,
                status=terminal,
                result=result,
                error_code=error_code,
                actual_cost=actual_cost,
            )
            return result
        except Exception:
            _job_store.update(job_id, status="failed", error_code="job_failed")
            raise RuntimeError("job_failed")

    _start_job(
        job_id,
        durable_work,
        jobs=_jobs,
        lock=_jobs_lock,
        evict=_evict_old_jobs,
    )


# ── Frontend templates & static files ──────────────────────────────────
# Templates live in ./templates/ (landing.html, app.html)
# CSS + JS live in ./static/ (served at /static/*)
LANDING_TEMPLATE = "landing.html"
APP_TEMPLATE = "app.html"


# ── API Routes ──────────────────────────────────────────────────────────


_SSRF_BLOCKED_SCHEMES = frozenset(("", "file", "ftp", "gopher", "ldap", "dict", "data", "javascript"))


def _is_safe_export_url(url: str) -> bool:
    """Only same-origin generated images are exportable.

    Remote fetching is intentionally not supported here: validating a DNS
    answer before a later fetch cannot safely prevent redirects or rebinding.
    """
    return bool(
        isinstance(url, str)
        and url.startswith("/image/")
        and "://" not in url
        and "\n" not in url
        and "\r" not in url
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


# ── Whoami / BYOK status (existing) ────────────────────────────────


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
    return _billing_service.configured()


def _stripe_api():
    """Return a configured stripe module. Raises RuntimeError if
    Stripe is not configured — callers should call _stripe_configured
    first and return 503 if false."""
    return _billing_service.stripe_api(_stripe_lib)


def _get_or_create_stripe_customer(user: dict) -> str:
    """Find the user's Stripe customer id, creating one if needed.
    Stores the id on the user row so we don't re-lookup on every
    request."""
    return _billing_service.get_or_create_customer(AUTH_DB, _stripe_api(), user)


def _resolve_price_id(plan: str) -> str:
    """Look up the Stripe price id for a plan from the env. Raises
    ValueError if the plan is unknown or the price id isn't set."""
    return _billing_service.resolve_price_id(plan, _BILLING_PLANS)


def _tier_from_price_id(price_id: str) -> str | None:
    return _billing_service.tier_from_price_id(price_id, _BILLING_PLANS)


def _record_subscription(user_id: str, sub: dict) -> None:
    _billing_service.record_subscription(AUTH_DB, user_id, sub, _BILLING_PLANS)


def _cancel_subscription(user_id: str) -> None:
    _billing_service.cancel_subscription(AUTH_DB, user_id)


def _top_up_credits(user_id: str) -> None:
    _billing_service.top_up_credits(AUTH_DB, user_id, _BILLING_PLANS)



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


app.register_blueprint(_create_seo_blueprint(lambda: BLOG_CONTENT_DIR))
app.register_blueprint(
    _create_core_blueprint(
        landing_template=LANDING_TEMPLATE,
        app_template=APP_TEMPLATE,
        require_access=_require_access,
        current_actor_id=_current_actor_id,
        job_store=_job_store,
        jobs=_jobs,
        jobs_lock=_jobs_lock,
        get_output_dir=lambda: OUTPUT_DIR,
        rate_limited=rate_limited,
    )
)
app.register_blueprint(
    _create_delivery_blueprint(
        safe_export_url=_is_safe_export_url,
        current_session=_current_session,
        current_actor_id=_current_actor_id,
        owned_asset_paths=_owned_asset_paths,
        shared_figma_enabled=lambda: os.environ.get("CREATIVE_ENABLE_SHARED_FIGMA_TOKEN") == "1" and _admin_authed(),
        require_api_key=_require_api_key,
        require_access=_require_access,
        get_api_key=_get_api_key,
        enforce_prompt_length=_enforce_prompt_length,
        enforce_daily_limit=enforce_daily_limit,
        safe_filename=_safe_filename,
        save_upload=_save_upload,
        safe_output_path=_safe_output_relpath,
        get_data_dir=lambda: DATA_DIR,
        new_session_id=new_session_id,
        run_composite=run_cli_composite,
        run_export=run_cli_export,
        run_qc=run_cli_qc,
        add_entry=add_entry,
        load_session=load_session,
        save_session=save_session,
        parse_figma_url=parse_figma_url,
        fetch_figma_context=fetch_figma_context,
        rate_limited=rate_limited,
        validate_version_parent=_validate_version_parent,
        current_version_node=_current_version_node,
        estimate_cost=cost_for_tier,
        record_provider_results=_record_provider_results,
        resolve_campaign_pack=_resolve_campaign_pack,
        record_campaign_qc=_record_campaign_qc,
        get_campaign=_get_campaign,
    )
)


def _fetch_owner_figma(owner_id, file_key, node_id):
    if not owner_id or not owner_id.startswith("user:"):
        return {"error": "Sign in and connect Figma first", "status": 401}
    if not _figma_oauth_configured:
        return {"error": "Figma OAuth is not configured", "status": 503}
    try:
        return _figma_owner_context(_figma_store, _figma_client, owner_id, file_key, node_id)
    except PermissionError as error:
        return {"error": str(error), "status": 401}
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            _figma_store.disconnect(owner_id)
            return {"error": "Figma authorization expired or was revoked", "status": 401}
        if error.code == 429:
            return {"error": "Figma rate limit reached; retry later", "status": 429}
        return {"error": "Figma request failed", "status": 502}
    except Exception:
        return {"error": "Figma context unavailable", "status": 502}


app.register_blueprint(
    _create_figma_oauth_blueprint(
        current_session=_current_session,
        store=_figma_store,
        client=_figma_client,
        configured=lambda: _figma_oauth_configured,
        parse_figma_url=parse_figma_url,
        rate_limited=rate_limited,
    )
)
app.register_blueprint(
    _create_generation_blueprint(
        enforce_prompt_length=_enforce_prompt_length,
        require_api_key=_require_api_key,
        enforce_daily_limit=enforce_daily_limit,
        parse_figma_url=parse_figma_url,
        fetch_figma_context=fetch_figma_context,
        enhance_prompt_with_figma=enhance_prompt_with_figma,
        new_session_id=new_session_id,
        new_job_id=_job_id,
        run_job_background=_run_job_background,
        jobs=_jobs,
        jobs_lock=_jobs_lock,
        run_generate=lambda *args, **kwargs: run_cli_generate(*args, **kwargs),
        add_entry=add_entry,
        load_costs=load_costs,
        save_costs=save_costs,
        get_sessions_dir=lambda: SESSIONS_DIR,
        current_session=_current_session,
        current_actor_id=_current_actor_id,
        job_store=_job_store,
        estimate_cost=cost_for_tier,
        max_job_cost=float(os.environ.get("CREATIVE_MAX_JOB_COST", "1.00")),
        rate_limited=rate_limited,
        durable_jobs_enabled=os.environ.get(
            "CREATIVE_DURABLE_JOBS_ENABLED", "true"
        ).lower() in ("1", "true", "yes"),
        validate_version_parent=_validate_version_parent,
        current_version_node=_current_version_node,
        record_provider_results=_record_provider_results,
        fetch_owner_figma_context=_fetch_owner_figma,
        require_access=_require_access,
        refund_credits=_refund_credits,
    )
)
app.register_blueprint(
    _create_iteration_blueprint(
        require_api_key=_require_api_key,
        current_actor_id=_current_actor_id,
        enforce_prompt_length=_enforce_prompt_length,
        enforce_daily_limit=enforce_daily_limit,
        build_pin_prompt=build_pin_prompt,
        safe_filename=_safe_filename,
        save_upload=_save_upload,
        get_data_dir=lambda: DATA_DIR,
        image_extensions=_IMAGE_EXTS,
        tier_models=_TIER_MODEL,
        scene_prompts=_SCENE_PROMPTS,
        scene_aspects=_SCENE_ASPECTS,
        scene_labels=_SCENE_LABELS,
        new_session_id=new_session_id,
        run_refine=lambda *args, **kwargs: run_cli_refine(*args, **kwargs),
        run_variations=lambda *args, **kwargs: run_cli_variations(*args, **kwargs),
        run_composite=lambda *args, **kwargs: run_cli_composite(*args, **kwargs),
        run_refine_from_variation=lambda **kwargs: run_cli_refine_from_variation(
            **kwargs
        ),
        add_entry=add_entry,
        rate_limited=rate_limited,
        validate_version_parent=_validate_version_parent,
        current_version_node=_current_version_node,
        estimate_cost=cost_for_tier,
        record_provider_results=_record_provider_results,
    )
)
app.register_blueprint(
    _create_account_blueprint(
        create_magic_link_token=_create_magic_link_token,
        deliver_magic_link=_deliver_magic_link,
        expose_magic_link_token=_expose_magic_link_token,
        consume_magic_link=_consume_magic_link,
        current_session=_current_session,
        auth_db=_auth_db,
        signup_enabled=lambda: os.environ.get(
            "CREATIVE_SIGNUP_ENABLED", "true"
        ).lower() in ("1", "true", "yes"),
    )
)
app.register_blueprint(
    _create_billing_blueprint(
        plans=_BILLING_PLANS,
        portal_return_url=_BILLING_PORTAL_RETURN_URL,
        auth_db_path=AUTH_DB,
        auth_db=_auth_db,
        current_session=_current_session,
        stripe_configured=_stripe_configured,
        stripe_api=_stripe_api,
        get_or_create_customer=_get_or_create_stripe_customer,
        resolve_price_id=_resolve_price_id,
        handle_event=_billing_service.handle_event,
        rate_limited=rate_limited,
    )
)
app.register_blueprint(
    _create_project_blueprint(
        current_session=_current_session,
        create_project=_create_project,
        list_projects=_list_projects_for_user,
        get_project=_get_project_for_user,
        delete_project=_delete_project,
        add_generation=_add_generation_to_project,
        safe_output_relpath=_safe_output_relpath,
        output_dir=OUTPUT_DIR,
        rate_limited=rate_limited,
        project_name_max=_PROJECT_NAME_MAX,
        generation_url_max=_GENERATION_URL_MAX,
        generation_prompt_max=_GENERATION_PROMPT_MAX,
    )
)
app.register_blueprint(
    _create_campaign_blueprint(
        current_session=_current_session,
        create_campaign=_create_campaign,
        list_campaigns=_list_campaigns,
        list_brand_passports=_list_brand_passports,
        list_product_truth=_list_product_truth,
        get_product_truth=_get_product_truth,
        create_product_claim=_create_product_claim,
        list_product_claims=_list_product_claims,
        retire_product_claim=_retire_product_claim,
        get_campaign=_get_campaign,
        campaign_readiness=_campaign_service.readiness,
        build_generation_plan=_campaign_service.generation_plan,
        mark_campaign_started=_mark_campaign_started,
        attach_pack_asset=_attach_campaign_pack,
        save_upload=_save_upload,
        owned_asset_paths=_owned_asset_paths,
        safe_output_path=_safe_output_relpath,
        build_campaign_bundle=_build_campaign_bundle,
        record_campaign_bundle=_record_campaign_bundle,
        list_campaign_bundles=_list_campaign_bundles,
        get_campaign_bundle=_get_campaign_bundle,
        campaign_bundle_path=_campaign_bundle_path,
        create_campaign_exception=_create_campaign_exception,
        list_campaign_exceptions=_list_campaign_exceptions,
        resolve_campaign_exception=_resolve_campaign_exception,
        campaign_exception_gate=_campaign_exception_gate,
        record_campaign_preflight=_record_campaign_preflight,
        rate_limited=rate_limited,
    )
)
app.register_blueprint(
    _create_library_blueprint(
        get_output_dir=lambda: OUTPUT_DIR,
        current_session=_current_session,
        owned_asset_paths=_owned_asset_paths,
        rate_limited=rate_limited,
    )
)
app.register_blueprint(
    _create_state_blueprint(
        require_access=_require_access,
        current_actor_id=_current_actor_id,
        safe_pin_path=_safe_pin_path,
        safe_pin_id=_safe_pin_id,
        load_pins=load_pins,
        save_pins=save_pins,
        new_pin_id=pin_id,
        now=now_str,
        get_sessions_dir=lambda: SESSIONS_DIR,
        load_json=load_json,
        load_session=load_session,
        load_costs=load_costs,
        rate_limited=rate_limited,
    )
)
app.register_blueprint(
    _create_version_blueprint(
        current_actor_id=_current_actor_id,
        graph_store=_version_store,
        load_session=load_session,
        asset_exists=_version_asset_exists,
        enabled=lambda: os.environ.get(
            "CREATIVE_VERSION_GRAPH_ENABLED", "true"
        ).lower() in ("1", "true", "yes"),
        rate_limited=rate_limited,
    )
)
app.register_blueprint(
    _create_provider_metrics_blueprint(
        ledger=_provider_ledger,
        admin_authed=_admin_authed,
        rate_limited=rate_limited,
    )
)
app.register_blueprint(
    _create_support_blueprint(
        waitlist_pattern=_WAITLIST_RE,
        request_lock=_request_log_lock,
        read_waitlist=_read_waitlist,
        write_waitlist=_write_waitlist,
        now=now_str,
        read_templates=_read_templates,
        server_api_key=SERVER_API_KEY,
        allow_server_fallback=ALLOW_SERVER_FALLBACK,
        version=__version__,
        admin_authed=_admin_authed,
        rate_limited=rate_limited,
    )
)
app.register_blueprint(
    _create_chat_blueprint(
        require_api_key=_require_api_key,
        require_access=_require_access,
        current_actor_id=_current_actor_id,
        enforce_prompt_length=_enforce_prompt_length,
        safe_filename=_safe_filename,
        save_upload=_save_upload,
        get_data_dir=lambda: DATA_DIR,
        get_chat_sessions=lambda: _chat_sessions,
        run_chat_turn=run_cli_chat_turn,
        chat_history=chat_session_history,
        reset_chat=chat_reset,
        new_session_id=new_session_id,
        add_entry=add_entry,
        image_url=image_url,
        rate_limited=rate_limited,
        validate_version_parent=_validate_version_parent,
        current_version_node=_current_version_node,
        estimate_cost=cost_for_tier,
        record_provider_results=_record_provider_results,
    )
)
app.register_blueprint(
    _create_informational_blueprint(
        load_costs=load_costs,
        jobs=_jobs,
        jobs_lock=_jobs_lock,
        get_sessions_dir=lambda: SESSIONS_DIR,
        load_json=load_json,
        admin_authed=_admin_authed,
    )
)


# ─── Main ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5173)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    print(f"🎨 Creative Studio Web App running at http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)
