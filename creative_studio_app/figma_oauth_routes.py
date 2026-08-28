"""Per-user Figma OAuth and owner-bound context retrieval."""

import time
import urllib.error
from collections.abc import Callable

from flask import Blueprint, jsonify, redirect, request


def owner_context(store, client, owner_id: str, file_key: str, node_id: str | None):
    """Fetch context with an owner's token, refreshing and redacting failures."""
    connection = store.connection(owner_id)
    if not connection:
        raise PermissionError("Connect Figma first")
    if connection["expires_at"] <= time.time() + 60:
        refreshed = client.refresh(connection["refresh_token"])
        refreshed.setdefault("refresh_token", connection["refresh_token"])
        refreshed.setdefault("user_id_string", connection["figma_user_id"])
        refreshed.setdefault("scope", connection["scope"])
        store.save_connection(owner_id, refreshed)
        connection = store.connection(owner_id)
    result = client.fetch_file(connection["access_token"], file_key, node_id)
    store.note_file_use(owner_id, file_key)
    store.audit(owner_id, "file_context", "ok")
    return result


def create_blueprint(
    *,
    current_session: Callable[[], dict | None],
    store,
    client,
    configured: Callable[[], bool],
    parse_figma_url: Callable,
    rate_limited: Callable,
) -> Blueprint:
    blueprint = Blueprint("figma_oauth", __name__)

    def owner():
        session = current_session()
        if not session:
            return None, (jsonify({"error": "Sign in required"}), 401)
        return f"user:{session['user_id']}", None

    @blueprint.post("/api/figma/oauth/connect")
    @rate_limited
    def connect():
        owner_id, error = owner()
        if error:
            return error
        if not configured():
            return jsonify({"error": "Figma OAuth is not configured"}), 503
        flow = store.begin(owner_id)
        return jsonify({
            "authorization_url": client.authorization_url(
                state=flow["state"], code_challenge=flow["code_challenge"]
            ),
            "scope": "file_content:read",
        })

    @blueprint.get("/api/figma/oauth/callback")
    @rate_limited
    def callback():
        if not configured():
            return jsonify({"error": "Figma OAuth is not configured"}), 503
        state = store.consume_state(request.args.get("state", ""))
        code = request.args.get("code", "")
        if not state or not code:
            return jsonify({"error": "Invalid or expired Figma authorization"}), 400
        try:
            tokens = client.exchange(code, state["code_verifier"])
            store.save_connection(state["owner_id"], tokens)
        except Exception:
            store.audit(state["owner_id"], "callback", "failed")
            return jsonify({"error": "Figma authorization failed"}), 502
        return redirect("/app?figma=connected", code=303)

    @blueprint.get("/api/figma/oauth/status")
    @rate_limited
    def status():
        owner_id, error = owner()
        if error:
            return error
        if not configured():
            return jsonify({"connected": False, "configured": False})
        try:
            connection = store.connection(owner_id)
        except ValueError:
            store.disconnect(owner_id)
            return jsonify({"connected": False, "configured": True, "reconnect_required": True})
        return jsonify({
            "connected": connection is not None,
            "configured": True,
            "scope": connection["scope"] if connection else None,
            "expires_at": connection["expires_at"] if connection else None,
        })

    @blueprint.post("/api/figma/oauth/disconnect")
    @rate_limited
    def disconnect():
        owner_id, error = owner()
        if error:
            return error
        if not configured():
            return jsonify({"error": "Figma OAuth is not configured"}), 503
        return jsonify({"disconnected": store.disconnect(owner_id)})

    @blueprint.post("/api/figma/context")
    @rate_limited
    def context():
        owner_id, error = owner()
        if error:
            return error
        if not configured():
            return jsonify({"error": "Figma OAuth is not configured"}), 503
        file_key, node_id = parse_figma_url((request.json or {}).get("url", ""))
        if not file_key:
            return jsonify({"error": "Invalid Figma URL"}), 400
        try:
            result = owner_context(store, client, owner_id, file_key, node_id)
            return jsonify(result)
        except PermissionError as access_error:
            return jsonify({"error": str(access_error)}), 401
        except urllib.error.HTTPError as provider_error:
            if provider_error.code in {401, 403}:
                store.disconnect(owner_id)
                return jsonify({"error": "Figma authorization expired or was revoked"}), 401
            if provider_error.code == 429:
                store.audit(owner_id, "file_context", "rate_limited")
                return jsonify({"error": "Figma rate limit reached; retry later"}), 429
            store.audit(owner_id, "file_context", "failed")
            return jsonify({"error": "Figma request failed"}), 502
        except Exception:
            store.audit(owner_id, "file_context", "unavailable")
            return jsonify({"error": "Figma service unavailable"}), 503

    return blueprint
