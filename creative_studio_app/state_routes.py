"""Pin annotations, sessions, and cost-summary Flask routes."""

from collections.abc import Callable
from datetime import datetime

from flask import Blueprint, jsonify, request


def create_blueprint(
    *,
    require_access: Callable,
    current_actor_id: Callable[[], str | None],
    safe_pin_path: Callable[[str], str],
    safe_pin_id: Callable[[str], str],
    load_pins: Callable[[str], list],
    save_pins: Callable[[str, list], None],
    new_pin_id: Callable[[], str],
    now: Callable[[], str],
    get_sessions_dir: Callable,
    load_json: Callable,
    load_session: Callable[[str], dict],
    load_costs: Callable[[], dict],
    rate_limited: Callable,
) -> Blueprint:
    blueprint = Blueprint("state", __name__)

    def authorized():
        actor_id = current_actor_id()
        if not actor_id:
            return None, (jsonify({"error": "Sign in or provide an API key"}), 401)
        if actor_id.startswith("user:"):
            return actor_id, None
        # Read/annotate endpoints never spend credits.
        return actor_id, require_access()

    @blueprint.post("/api/pins")
    @rate_limited
    def add_pin():
        actor_id, error = authorized()
        if error is not None:
            return error
        data = request.json or {}
        image_path = safe_pin_path(data.get("image_path", ""))
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
        image_path = f"{actor_id}/{image_path}"
        pins = load_pins(image_path)
        pins.append({"id": new_pin_id(), "x": x, "y": y, "text": text, "time": now()})
        save_pins(image_path, pins)
        return jsonify({"pins": pins})

    @blueprint.get("/api/pins/<path:image_path>")
    @rate_limited
    def get_pins(image_path):
        actor_id, error = authorized()
        if error is not None:
            return error
        image_path = safe_pin_path(image_path)
        if not image_path:
            return jsonify({"error": "image_path required"}), 400
        return jsonify({"pins": load_pins(f"{actor_id}/{image_path}")})

    @blueprint.delete("/api/pins/<path:image_path>/<pin_id>")
    @rate_limited
    def delete_pin(image_path, pin_id):
        actor_id, error = authorized()
        if error is not None:
            return error
        image_path = safe_pin_path(image_path)
        if not image_path:
            return jsonify({"error": "image_path required"}), 400
        pin_id = safe_pin_id(pin_id)
        if not pin_id:
            return jsonify({"error": "pin_id must be hex (1-16 chars)"}), 400
        image_path = f"{actor_id}/{image_path}"
        pins = [pin for pin in load_pins(image_path) if pin.get("id") != pin_id]
        save_pins(image_path, pins)
        return jsonify({"pins": pins})

    @blueprint.delete("/api/pins/<path:image_path>")
    @rate_limited
    def clear_pins(image_path):
        actor_id, error = authorized()
        if error is not None:
            return error
        image_path = safe_pin_path(image_path)
        if not image_path:
            return jsonify({"error": "image_path required"}), 400
        save_pins(f"{actor_id}/{image_path}", [])
        return jsonify({"pins": []})

    @blueprint.get("/api/sessions")
    @rate_limited
    def sessions():
        actor_id, error = authorized()
        if error is not None:
            return error
        result = []
        for path in sorted(
            get_sessions_dir().glob("*.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        ):
            data = load_json(path)
            if data.get("owner_id") != actor_id:
                continue
            entries = data.get("entries", [])
            result.append(
                {
                    "id": data.get("id", path.stem),
                    "created_at": data.get("created_at", ""),
                    "entries": entries,
                    "cost": sum(entry.get("cost", 0) for entry in entries),
                }
            )
        return jsonify({"sessions": result})

    @blueprint.get("/api/session/<session_id>")
    @rate_limited
    def session(session_id):
        actor_id, error = authorized()
        if error is not None:
            return error
        data = load_session(session_id)
        if data.get("owner_id") != actor_id:
            return jsonify({"error": "Session not found"}), 404
        entries = []
        for entry in data.get("entries", []):
            copy = dict(entry)
            copy["image_url"] = entry.get("image_url", "")
            entries.append(copy)
        return jsonify(
            {
                "id": session_id,
                "entries": entries,
                "created_at": data.get("created_at", ""),
            }
        )

    @blueprint.get("/api/costs")
    @rate_limited
    def costs():
        actor_id, error = authorized()
        if error is not None:
            return error
        owned_sessions = [
            load_json(path) for path in get_sessions_dir().glob("*.json")
            if load_json(path).get("owner_id") == actor_id
        ]
        total = sum(
            entry.get("cost", 0)
            for session_data in owned_sessions
            for entry in session_data.get("entries", [])
        )
        result = {"total": total, "by_date": {}, "by_tier": {}}
        result["session_count"] = len(owned_sessions)
        today = datetime.now().strftime("%Y-%m-%d")
        result["today"] = result.get("by_date", {}).get(today, 0.0)
        return jsonify(result)

    return blueprint
