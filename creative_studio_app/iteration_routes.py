"""Refine, variation, and scene-set Flask routes."""

import sys
import threading
import time
from collections.abc import Callable

from flask import Blueprint, jsonify, request


def create_blueprint(
    *,
    require_api_key: Callable,
    enforce_prompt_length: Callable,
    enforce_daily_limit: Callable,
    build_pin_prompt: Callable[[list], str],
    safe_filename: Callable[[str], str],
    save_upload: Callable,
    get_data_dir: Callable,
    image_extensions: set,
    tier_models: dict,
    scene_prompts: dict,
    scene_aspects: dict,
    scene_labels: dict,
    new_session_id: Callable[[], str],
    run_refine: Callable,
    run_variations: Callable,
    run_composite: Callable,
    run_refine_from_variation: Callable,
    add_entry: Callable[[str, dict], None],
    rate_limited: Callable,
) -> Blueprint:
    blueprint = Blueprint("iteration_routes", __name__)

    @blueprint.post("/api/refine")
    @rate_limited
    def refine():
        api_key, error, _used_trial_credit = require_api_key()
        if error is not None:
            return error
        data = request.json or {}
        changes = data.get("changes", "").strip()
        pin_text = build_pin_prompt(data.get("pins", [])) if data.get("pins") else ""
        full_changes = (
            f"{changes}. Also: {pin_text}"
            if changes and pin_text
            else pin_text or changes
        )
        if not full_changes:
            return jsonify({"error": "changes or pins required"}), 400
        length_error = enforce_prompt_length(full_changes)
        if length_error is not None:
            return length_error
        session_id = data.get("session_id", new_session_id())
        images = run_refine(
            data.get("image_path", ""),
            full_changes,
            api_key,
            data.get("tier", "quality"),
        )
        for image in images:
            add_entry(
                session_id,
                {
                    "type": "refine",
                    "cost": image.get("cost", 0),
                    "image_url": image.get("url", ""),
                    "model": image.get("model", ""),
                    "note": full_changes[:200],
                },
            )
        return jsonify({"message": "Refined", "images": images, "session_id": session_id})

    @blueprint.post("/api/variations")
    @rate_limited
    def variations():
        api_key, error, _used_trial_credit = require_api_key()
        if error is not None:
            return error
        data = request.form if request.files else (request.json or {})
        prompt = data.get("prompt", "").strip()
        if not prompt:
            return jsonify({"error": "Prompt required"}), 400
        length_error = enforce_prompt_length(prompt)
        if length_error is not None:
            return length_error
        try:
            count = int(data.get("count", 4))
        except (TypeError, ValueError):
            return jsonify({"error": "count must be an integer from 1 to 8"}), 400
        if not 1 <= count <= 8:
            return jsonify({"error": "count must be between 1 and 8"}), 400
        tier = data.get("tier", "balanced")
        limit_error = enforce_daily_limit(count, tier)
        if limit_error is not None:
            return limit_error
        input_image = None
        if "image" in request.files:
            try:
                input_image = str(save_upload(request.files["image"], "variations_ref"))
            except ValueError as error:
                return jsonify({"error": str(error)}), 400
        session_id = data.get("session_id", new_session_id())
        images, session_key = run_variations(
            api_key,
            prompt=prompt,
            count=count,
            tier=tier,
            aspect=data.get("aspect_ratio", "1:1"),
            input_image=input_image,
        )
        for image in images:
            if "error" not in image:
                add_entry(
                    session_id,
                    {
                        "type": "variations",
                        "prompt": prompt[:100],
                        "cost": image.get("cost", 0),
                        "image_url": image.get("url", ""),
                        "model": image.get("model", ""),
                        "note": f"v{image.get('variation_index', '?')}",
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

    @blueprint.post("/api/scene-set")
    @rate_limited
    def scene_set():
        api_key, error, _used_trial_credit = require_api_key()
        if error is not None:
            return error
        if "product" not in request.files:
            return jsonify({"error": "Product image required (form field 'product')"}), 400
        upload = request.files["product"]
        tier = request.form.get("tier", "balanced")
        if tier not in tier_models:
            tier = "balanced"
        limit_error = enforce_daily_limit(len(scene_prompts), tier)
        if limit_error is not None:
            return limit_error
        session_id = request.form.get("session_id", new_session_id())
        try:
            product = save_upload(upload, "sceneset")
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
        images = []
        images_lock = threading.Lock()
        scenes = list(scene_prompts)

        def run_scene(scene):
            prompt = scene_prompts[scene]
            aspect = scene_aspects[scene]
            try:
                result = run_composite(
                    prompt,
                    str(product),
                    api_key,
                    aspect,
                    name_suffix=scene,
                )
                if not result or "error" in result[0]:
                    return
                image = result[0]
                normalized = {
                    "scene": scene,
                    "label": scene_labels[scene],
                    "url": image.get("url", ""),
                    "path": image.get("path", ""),
                    "name": image.get("name", ""),
                    "cost": image.get("cost", 0),
                    "model": image.get("model", ""),
                    "ratio": aspect,
                }
                with images_lock:
                    images.append(normalized)
                    add_entry(
                        session_id,
                        {
                            "type": "sceneset",
                            "prompt": prompt[:100],
                            "cost": image.get("cost", 0),
                            "image_url": image.get("url", ""),
                            "model": image.get("model", ""),
                            "note": scene_labels[scene],
                        },
                    )
            except Exception as error:
                print(f"[sceneset] {scene} failed: {error}", file=sys.stderr)

        threads = [
            threading.Thread(target=run_scene, args=(scene,), daemon=True)
            for scene in scenes
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=300)
        images.sort(key=lambda image: scenes.index(image["scene"]))
        return jsonify(
            {
                "message": f"Generated {len(images)}/{len(scenes)} scene(s)",
                "images": images,
                "session_id": session_id,
                "total_cost": sum(image.get("cost", 0) for image in images),
                "scenes_requested": scenes,
            }
        )

    @blueprint.post("/api/variations/<session_key>/refine")
    @rate_limited
    def refine_variation(session_key):
        api_key, error, _used_trial_credit = require_api_key()
        if error is not None:
            return error
        data = request.json or {}
        changes = data.get("changes", "").strip()
        if not changes:
            return jsonify({"error": "changes required"}), 400
        length_error = enforce_prompt_length(changes)
        if length_error is not None:
            return length_error
        try:
            pick = int(data.get("pick", 1))
        except (TypeError, ValueError):
            return jsonify({"error": "pick must be a positive integer"}), 400
        if pick < 1:
            return jsonify({"error": "pick must be a positive integer"}), 400
        session_id = data.get("session_id", new_session_id())
        images = run_refine_from_variation(
            session_key=session_key,
            pick_index=pick,
            changes=changes,
            tier=data.get("tier", "quality"),
            api_key=api_key,
        )
        for image in images:
            if "error" not in image:
                add_entry(
                    session_id,
                    {
                        "type": "refine",
                        "prompt": f"Refine v{pick}: {changes[:80]}",
                        "cost": image.get("cost", 0),
                        "image_url": image.get("url", ""),
                        "model": image.get("model", ""),
                        "note": f"Refined from v{pick}",
                    },
                )
        return jsonify({"message": "Refined", "images": images, "session_id": session_id})

    return blueprint
