"""Multi-turn image-chat Flask routes."""

import shutil
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from flask import Blueprint, jsonify, request


def create_blueprint(
    *,
    require_api_key: Callable,
    enforce_prompt_length: Callable,
    safe_filename: Callable[[str], str],
    get_data_dir: Callable,
    get_chat_sessions: Callable[[], dict],
    run_chat_turn: Callable,
    chat_history: Callable[[str], list],
    reset_chat: Callable[[str], dict],
    new_session_id: Callable[[], str],
    add_entry: Callable[[str, dict], None],
    image_url: Callable[[str], str],
    rate_limited: Callable,
) -> Blueprint:
    blueprint = Blueprint("chat_routes", __name__)

    def authorized():
        key, error, _used_trial_credit = require_api_key()
        return key, error

    @blueprint.post("/api/chat")
    @rate_limited
    def chat():
        api_key, error = authorized()
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
        session_id = data.get("session_id", new_session_id())
        input_image = None
        if "image" in request.files:
            upload = request.files["image"]
            uploads = get_data_dir() / "uploads"
            uploads.mkdir(exist_ok=True)
            input_image = str(
                uploads / f"chat_ref_{int(time.time())}_{safe_filename(upload.filename)}"
            )
            upload.save(input_image)
        images, session = run_chat_turn(
            api_key,
            session_key=session_key,
            prompt=prompt,
            tier=data.get("tier", "balanced"),
            aspect=data.get("aspect_ratio", "1:1"),
            input_image=input_image,
        )
        for image in images:
            if "error" not in image:
                add_entry(
                    session_id,
                    {
                        "type": "chat",
                        "prompt": prompt[:100],
                        "cost": image.get("cost", 0),
                        "image_url": image.get("url", ""),
                        "model": image.get("model", ""),
                        "note": f"Turn {image.get('turn', '?')}",
                    },
                )
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
        _api_key, error = authorized()
        if error is not None:
            return error
        session = get_chat_sessions().get(session_key, {})
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
        _api_key, error = authorized()
        if error is not None:
            return error
        reset_chat(session_key)
        return jsonify({"message": "Chat session reset", "turn": 0})

    @blueprint.post("/api/chat/<session_key>/save")
    @rate_limited
    def save(session_key):
        _api_key, error = authorized()
        if error is not None:
            return error
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
