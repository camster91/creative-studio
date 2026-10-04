"""Multi-turn image-chat Flask routes."""

import shutil
import time
import uuid
import re
import time
from collections.abc import Callable
from pathlib import Path

from flask import Blueprint, jsonify, request

from .billing import charge_for_images, unknown_tier_error


def create_blueprint(
    *,
    require_api_key: Callable,
    require_access: Callable,
    current_actor_id: Callable[[], str | None],
    enforce_prompt_length: Callable,
    safe_filename: Callable[[str], str],
    save_upload: Callable,
    get_data_dir: Callable,
    get_chat_sessions: Callable[[], dict],
    run_chat_turn: Callable,
    chat_history: Callable[[str], list],
    reset_chat: Callable[[str], dict],
    new_session_id: Callable[[], str],
    add_entry: Callable[[str, dict], None],
    image_url: Callable[[str], str],
    rate_limited: Callable,
    validate_version_parent: Callable = lambda *_args: True,
    current_version_node: Callable = lambda *_args: None,
    estimate_cost: Callable = lambda _tier: 0,
    record_provider_results: Callable = lambda *_args, **_kwargs: None,
    current_session: Callable[[], dict | None] = lambda: None,
    refund_credits: Callable[[str, int], None] = lambda *_args: None,
) -> Blueprint:
    blueprint = Blueprint("chat_routes", __name__)

    def reader():
        # History / reset / save never generate, so they never spend credits.
        return current_actor_id(), require_access()

    def owned_chat(session_key: str, actor_id: str):
        session = get_chat_sessions().get(session_key)
        if session is not None and session.get("_owner_id") != actor_id:
            return None
        return session

    @blueprint.post("/api/chat")
    @rate_limited
    def chat():
        actor_id, error = reader()
        if error is not None:
            return error
        data = request.form if request.files else (request.json or {})
        prompt = data.get("prompt", "").strip()
        if not prompt:
            return jsonify({"error": "Prompt required"}), 400
        length_error = enforce_prompt_length(prompt)
        if length_error is not None:
            return length_error
        session_key = data.get("session_key", f"chat-{uuid.uuid4().hex[:8]}")
        if not isinstance(session_key, str) or not re.fullmatch(r"chat-[0-9a-f]{8}", session_key):
            return jsonify({"error": "Invalid chat session key"}), 400
        if owned_chat(session_key, actor_id) is None and session_key in get_chat_sessions():
            return jsonify({"error": "Chat session not found"}), 404
        tier = data.get("tier", "balanced")
        tier_error = unknown_tier_error(tier)
        if tier_error is not None:
            return jsonify(tier_error), 400
        session_id = data.get("session_id", new_session_id())
        parent_node_id = data.get("parent_node_id") or None
        parent_node_id = parent_node_id or current_version_node(session_id, actor_id)
        if not validate_version_parent(session_id, actor_id, parent_node_id):
            return jsonify({"error": "Parent version not found"}), 404
        input_image = None
        if "image" in request.files:
            try:
                input_image = str(save_upload(request.files["image"], "chat_ref"))
            except ValueError as error:
                return jsonify({"error": str(error)}), 400
        # One image per turn, charged at the turn's tier after validation.
        api_key, error, refund = charge_for_images(
            require_api_key, current_session, refund_credits, tier, 1
        )
        if error is not None:
            return error
        call_started = time.monotonic()
        images, session = run_chat_turn(
            api_key,
            session_key=session_key,
            prompt=prompt,
            tier=tier,
            aspect=data.get("aspect_ratio", "1:1"),
            input_image=input_image,
        )
        refund(1 - sum(1 for image in images if "error" not in image))
        record_provider_results(
            actor_id, session_id, images,
            estimated_cost_each=estimate_cost(tier),
            latency_ms=(time.monotonic() - call_started) * 1000,
        )
        session["_owner_id"] = actor_id
        for image in images:
            if "error" not in image:
                node_id = add_entry(
                    session_id,
                    {
                        "type": "chat",
                        "prompt": prompt[:100],
                        "cost": image.get("cost", 0),
                        "image_url": image.get("url", ""),
                        "model": image.get("model", ""),
                        "note": f"Turn {image.get('turn', '?')}",
                        "parent_node_id": parent_node_id,
                    },
                    actor_id,
                )
                image["version_node_id"] = node_id
        return jsonify(
            {
                "message": "Turn complete",
                "images": images,
                "session_key": session_key,
                "session_id": session_id,
                "turn": session.get("turn", 0),
            }
        )

    @blueprint.get("/api/chat/<session_key>/history")
    @rate_limited
    def history(session_key):
        actor_id, error = reader()
        if error is not None:
            return error
        session = owned_chat(session_key, actor_id)
        if session is None:
            return jsonify({"error": "Chat session not found"}), 404
        return jsonify(
            {
                "history": chat_history(session_key),
                "turn": session.get("turn", 0),
                "current_input": session.get("current_input"),
            }
        )

    @blueprint.post("/api/chat/<session_key>/reset")
    @rate_limited
    def reset(session_key):
        actor_id, error = reader()
        if error is not None:
            return error
        if owned_chat(session_key, actor_id) is None:
            return jsonify({"error": "Chat session not found"}), 404
        reset_chat(session_key)
        return jsonify({"message": "Chat session reset", "turn": 0})

    @blueprint.post("/api/chat/<session_key>/save")
    @rate_limited
    def save(session_key):
        actor_id, error = reader()
        if error is not None:
            return error
        if owned_chat(session_key, actor_id) is None:
            return jsonify({"error": "Chat session not found"}), 404
        raw_name = (request.json or {}).get("name", "").strip()
        name = safe_filename(raw_name or f"chat-{int(time.time())}")
        current_input = get_chat_sessions().get(session_key, {}).get("current_input")
        if not current_input or not Path(current_input).is_file():
            return jsonify({"error": "No output to save"}), 400
        approved = get_data_dir() / "approved"
        approved.mkdir(exist_ok=True)
        destination = approved / f"{name}.png"
        shutil.copy2(current_input, destination)
        return jsonify(
            {
                "message": f"Saved as {name}",
                "path": str(destination),
                "url": image_url(str(destination)),
            }
        )

    return blueprint
