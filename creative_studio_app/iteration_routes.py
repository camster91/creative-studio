"""Refine, variation, and scene-set Flask routes."""

import sys
import threading
import time
from collections.abc import Callable

from flask import Blueprint, jsonify, request

from .billing import charge_for_images, unknown_tier_error

# /api/scene-set composites every scene at run_composite's tier; the form
# tier is not forwarded to the generator, so credits are charged at this one.
SCENE_SET_TIER = "quality"


def create_blueprint(
    *,
    require_api_key: Callable,
    current_actor_id: Callable[[], str | None],
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
    validate_version_parent: Callable = lambda *_args: True,
    current_version_node: Callable = lambda *_args: None,
    estimate_cost: Callable = lambda _tier: 0,
    record_provider_results: Callable = lambda *_args, **_kwargs: None,
    require_access: Callable = lambda: None,
    current_session: Callable[[], dict | None] = lambda: None,
    refund_credits: Callable[[str, int], None] = lambda *_args: None,
) -> Blueprint:
    blueprint = Blueprint("iteration_routes", __name__)

    def charge(tier: str, images: int):
        # Spend credits only after the request has been validated.
        return charge_for_images(require_api_key, current_session, refund_credits, tier, images)

    def delivered(images) -> int:
        return sum(1 for image in images if "error" not in image)

    @blueprint.post("/api/refine")
    @rate_limited
    def refine():
        error = require_access()
        if error is not None:
            return error
        data = request.json or {}
        owner_id = current_actor_id()
        parent_node_id = data.get("parent_node_id") or None
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
        tier = data.get("tier", "quality")
        tier_error = unknown_tier_error(tier)
        if tier_error is not None:
            return jsonify(tier_error), 400
        session_id = data.get("session_id", new_session_id())
        parent_node_id = parent_node_id or current_version_node(session_id, owner_id)
        if not validate_version_parent(session_id, owner_id, parent_node_id):
            return jsonify({"error": "Parent version not found"}), 404
        api_key, error, refund = charge(tier, 1)
        if error is not None:
            return error
        call_started = time.monotonic()
        images = run_refine(
            data.get("image_path", ""),
            full_changes,
            api_key,
            tier,
        )
        refund(1 - delivered(images))
        record_provider_results(
            owner_id, session_id, images, estimated_cost_each=estimate_cost(tier),
            latency_ms=(time.monotonic() - call_started) * 1000,
        )
        for image in images:
            node_id = add_entry(
                session_id,
                {
                    "type": "refine",
                    "cost": image.get("cost", 0),
                    "image_url": image.get("url", ""),
                    "model": image.get("model", ""),
                    "note": full_changes[:200],
                    "parent_node_id": parent_node_id,
                },
                owner_id,
            )
            image["version_node_id"] = node_id
        return jsonify({"message": "Refined", "images": images, "session_id": session_id})

    @blueprint.post("/api/variations")
    @rate_limited
    def variations():
        error = require_access()
        if error is not None:
            return error
        data = request.form if request.files else (request.json or {})
        owner_id = current_actor_id()
        parent_node_id = data.get("parent_node_id") or None
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
        tier_error = unknown_tier_error(tier)
        if tier_error is not None:
            return jsonify(tier_error), 400
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
        parent_node_id = parent_node_id or current_version_node(session_id, owner_id)
        if not validate_version_parent(session_id, owner_id, parent_node_id):
            return jsonify({"error": "Parent version not found"}), 404
        api_key, error, refund = charge(tier, count)
        if error is not None:
            return error
        call_started = time.monotonic()
        images, session_key = run_variations(
            api_key,
            prompt=prompt,
            count=count,
            tier=tier,
            aspect=data.get("aspect_ratio", "1:1"),
            input_image=input_image,
        )
        record_provider_results(
            owner_id, session_id, images,
            estimated_cost_each=estimate_cost(tier),
            latency_ms=(time.monotonic() - call_started) * 1000,
        )
        images = [image for image in images if "error" not in image]
        refund(count - len(images))
        for image in images:
            if "error" not in image:
                node_id = add_entry(
                    session_id,
                    {
                        "type": "variations",
                        "prompt": prompt[:100],
                        "cost": image.get("cost", 0),
                        "image_url": image.get("url", ""),
                        "model": image.get("model", ""),
                        "note": f"v{image.get('variation_index', '?')}",
                        "parent_node_id": parent_node_id,
                    },
                    owner_id,
                )
                image["version_node_id"] = node_id
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
        error = require_access()
        if error is not None:
            return error
        if "product" not in request.files:
            return jsonify({"error": "Product image required (form field 'product')"}), 400
        upload = request.files["product"]
        owner_id = current_actor_id()
        parent_node_id = request.form.get("parent_node_id") or None
        tier = request.form.get("tier", "balanced")
        if tier not in tier_models:
            tier = "balanced"
        limit_error = enforce_daily_limit(len(scene_prompts), tier)
        if limit_error is not None:
            return limit_error
        session_id = request.form.get("session_id", new_session_id())
        parent_node_id = parent_node_id or current_version_node(session_id, owner_id)
        if not validate_version_parent(session_id, owner_id, parent_node_id):
            return jsonify({"error": "Parent version not found"}), 404
        try:
            product = save_upload(upload, "sceneset")
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
        api_key, error, refund = charge(SCENE_SET_TIER, len(scene_prompts))
        if error is not None:
            return error
        images = []
        images_lock = threading.Lock()
        scenes = list(scene_prompts)

        def run_scene(scene):
            prompt = scene_prompts[scene]
            aspect = scene_aspects[scene]
            try:
                call_started = time.monotonic()
                result = run_composite(
                    prompt,
                    str(product),
                    api_key,
                    aspect,
                    tier=SCENE_SET_TIER,
                    name_suffix=scene,
                )
                record_provider_results(
                    owner_id, session_id, result, estimated_cost_each=estimate_cost(tier),
                    latency_ms=(time.monotonic() - call_started) * 1000,
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
                    node_id = add_entry(
                        session_id,
                        {
                            "type": "sceneset",
                            "prompt": prompt[:100],
                            "cost": image.get("cost", 0),
                            "image_url": image.get("url", ""),
                            "model": image.get("model", ""),
                            "note": scene_labels[scene],
                            "parent_node_id": parent_node_id,
                        },
                        owner_id,
                    )
                    normalized["version_node_id"] = node_id
            except Exception:
                print(f"[sceneset] {scene} failed", file=sys.stderr)

        threads = [
            threading.Thread(target=run_scene, args=(scene,), daemon=True)
            for scene in scenes
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=300)
        images.sort(key=lambda image: scenes.index(image["scene"]))
        refund(len(scenes) - len(images))
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
        error = require_access()
        if error is not None:
            return error
        data = request.json or {}
        owner_id = current_actor_id()
        parent_node_id = data.get("parent_node_id") or None
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
        tier = data.get("tier", "quality")
        tier_error = unknown_tier_error(tier)
        if tier_error is not None:
            return jsonify(tier_error), 400
        session_id = data.get("session_id", new_session_id())
        parent_node_id = parent_node_id or current_version_node(session_id, owner_id)
        if not validate_version_parent(session_id, owner_id, parent_node_id):
            return jsonify({"error": "Parent version not found"}), 404
        api_key, error, refund = charge(tier, 1)
        if error is not None:
            return error
        call_started = time.monotonic()
        images = run_refine_from_variation(
            session_key=session_key,
            pick_index=pick,
            changes=changes,
            tier=tier,
            api_key=api_key,
        )
        refund(1 - delivered(images))
        record_provider_results(
            owner_id, session_id, images,
            estimated_cost_each=estimate_cost(tier),
            latency_ms=(time.monotonic() - call_started) * 1000,
        )
        for image in images:
            if "error" not in image:
                node_id = add_entry(
                    session_id,
                    {
                        "type": "refine",
                        "prompt": f"Refine v{pick}: {changes[:80]}",
                        "cost": image.get("cost", 0),
                        "image_url": image.get("url", ""),
                        "model": image.get("model", ""),
                        "note": f"Refined from v{pick}",
                        "parent_node_id": parent_node_id,
                    },
                    owner_id,
                )
                image["version_node_id"] = node_id
        return jsonify({"message": "Refined", "images": images, "session_id": session_id})

    return blueprint
