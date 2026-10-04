"""Primary image-generation Flask route."""

from collections.abc import Callable
import hashlib
import json
import time

from flask import Blueprint, jsonify, request

from .billing import CREDIT_WEIGHT_BY_TIER


def create_blueprint(
    *,
    enforce_prompt_length: Callable,
    require_api_key: Callable,
    enforce_daily_limit: Callable,
    parse_figma_url: Callable,
    fetch_figma_context: Callable,
    enhance_prompt_with_figma: Callable,
    new_session_id: Callable[[], str],
    new_job_id: Callable[[], str],
    run_job_background: Callable,
    jobs: dict,
    jobs_lock,
    run_generate: Callable,
    add_entry: Callable[[str, dict], None],
    load_costs: Callable[[], dict],
    save_costs: Callable[[dict], None],
    get_sessions_dir: Callable,
    current_session: Callable[[], dict | None],
    current_actor_id: Callable[[], str | None],
    job_store,
    estimate_cost: Callable[[str], float],
    max_job_cost: float,
    rate_limited: Callable,
    durable_jobs_enabled: bool = True,
    validate_version_parent: Callable = lambda *_args: True,
    current_version_node: Callable = lambda *_args: None,
    record_provider_results: Callable = lambda *_args, **_kwargs: None,
    fetch_owner_figma_context: Callable | None = None,
    require_access: Callable = lambda: None,
    refund_credits: Callable[[str, int], None] = lambda *_args: None,
    credit_weights: dict = CREDIT_WEIGHT_BY_TIER,
) -> Blueprint:
    blueprint = Blueprint("generation_routes", __name__)

    def record(session_id: str, mode: str, prompt: str, aspect: str, image: dict, owner_id: str, parent_node_id=None):
        node_id = add_entry(
            session_id,
            {
                "type": mode,
                "prompt": prompt[:100],
                "cost": image.get("cost", 0),
                "image_url": image.get("url", ""),
                "model": image.get("model", ""),
                "ratio": image.get("ratio", aspect),
                "note": f"{image.get('name', '')} ({image.get('model', '')})",
                "parent_node_id": parent_node_id,
            },
            owner_id,
        )
        image["version_node_id"] = node_id

    def persist_session_count():
        costs = load_costs()
        costs["session_count"] = len(list(get_sessions_dir().glob("*.json")))
        save_costs(costs)

    @blueprint.post("/api/generate")
    @rate_limited
    def generate():
        data = request.json or {}
        prompt = data.get("prompt", "").strip()
        if not prompt:
            return jsonify({"error": "Prompt required"}), 400
        length_error = enforce_prompt_length(prompt)
        if length_error is not None:
            return length_error
        # Cheap, non-spending auth gate first; credits are only charged
        # once every input below has been validated.
        access_error = require_access()
        if access_error is not None:
            return access_error
        owner_id = current_actor_id()
        parent_node_id = data.get("parent_node_id") or None
        raw_variations = data.get("variations", 1)
        if isinstance(raw_variations, bool):
            return jsonify({"error": "variations must be an integer from 1 to 8"}), 400
        try:
            variations = int(raw_variations)
        except (TypeError, ValueError):
            return jsonify({"error": "variations must be an integer from 1 to 8"}), 400
        if not 1 <= variations <= 8:
            return jsonify({"error": "variations must be between 1 and 8"}), 400
        mode = data.get("mode", "direct")
        tier = data.get("tier", "balanced")
        if tier not in credit_weights:
            return jsonify({
                "error": "Unknown quality tier",
                "valid_tiers": list(credit_weights),
            }), 400
        aspect = data.get("aspect_ratio", "16:9")
        session_id = data.get("session_id", new_session_id())
        batch_mode = variations > 1 and durable_jobs_enabled
        if batch_mode:
            estimated_cost = estimate_cost(tier) * variations
            if max_job_cost > 0 and estimated_cost > max_job_cost:
                return jsonify({
                    "error": "Request exceeds per-job cost limit",
                    "estimated_cost": round(estimated_cost, 4),
                    "job_cost_limit": max_job_cost,
                }), 400
            idempotency_key = (
                request.headers.get("Idempotency-Key")
                or data.get("idempotency_key")
                or new_job_id()
            )
            if not isinstance(idempotency_key, str) or not idempotency_key.isascii() or len(idempotency_key) > 128:
                return jsonify({"error": "Invalid idempotency key"}), 400
        parent_node_id = parent_node_id or current_version_node(session_id, owner_id)
        if not validate_version_parent(session_id, owner_id, parent_node_id):
            return jsonify({"error": "Parent version not found"}), 404
        limit_error = enforce_daily_limit(variations, tier)
        if limit_error is not None:
            return limit_error

        # Charge per image x tier weight, all-or-nothing.
        credits_per_image = credit_weights[tier]
        credits_charged = credits_per_image * variations
        api_key, error, used_trial_credit = require_api_key(credits=credits_charged)
        if error is not None:
            return error
        billed = current_session() if used_trial_credit else None
        billed_user_id = billed["user_id"] if billed else None

        def refund(undelivered_images: int):
            if billed_user_id and undelivered_images > 0:
                refund_credits(billed_user_id, credits_per_image * undelivered_images)

        figma_url = data.get("figma_url")
        if figma_url:
            file_key, node_id = parse_figma_url(figma_url)
            if file_key:
                context = (
                    fetch_owner_figma_context(owner_id, file_key, node_id)
                    if fetch_owner_figma_context else fetch_figma_context(file_key, node_id)
                )
                if "error" not in context:
                    prompt = enhance_prompt_with_figma(prompt, context)
                    length_error = enforce_prompt_length(prompt)
                    if length_error is not None:
                        refund(variations)
                        return length_error
                elif fetch_owner_figma_context:
                    refund(variations)
                    return jsonify(context), int(context.get("status", 502))

        if batch_mode:
            owner_id = current_actor_id()
            fingerprint = hashlib.sha256(json.dumps({
                "prompt": prompt,
                "mode": mode,
                "tier": tier,
                "aspect": aspect,
                "variations": variations,
                "session_id": session_id,
            }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            try:
                job, created = job_store.create_or_get(
                    owner_id, idempotency_key, fingerprint, estimated_cost
                )
            except ValueError as error:
                refund(variations)
                return jsonify({"error": str(error)}), 409
            job_id = job["id"]
            if not created:
                # The original request already paid for this job.
                refund(variations)
                return jsonify({
                    "job_id": job_id,
                    "status": job["status"],
                    "idempotency_key": idempotency_key,
                    "idempotent_replay": True,
                    "estimated_cost": job["estimated_cost"],
                })

            def generate_batch():
                images = []
                for index in range(variations):
                    if job_store.cancellation_requested(job_id):
                        refund(variations - len(images))
                        return {
                            "_job_status": "cancelled",
                            "images": images,
                            "session_id": session_id,
                            "message": f"Cancelled after {len(images)} image(s)",
                        }
                    call_started = time.monotonic()
                    batch = run_generate(
                        prompt, mode, api_key, tier, aspect, True, variations=1
                    )
                    record_provider_results(
                        owner_id, job_id, batch,
                        estimated_cost_each=estimate_cost(tier),
                        latency_ms=(time.monotonic() - call_started) * 1000,
                    )
                    if not batch or "error" in batch[0]:
                        failure = batch[0] if batch else {}
                        refund(variations - len(images))
                        return {
                            "_job_status": "failed",
                            "_job_error_code": failure.get("error_code", "provider_failed"),
                            "images": images,
                            "session_id": session_id,
                            "message": "Image provider failed before the batch completed",
                        }
                    image = batch[0]
                    record(
                        session_id, mode, prompt, aspect, image, owner_id,
                        parent_node_id,
                    )
                    images.append(image)
                    partial = {
                        "images": images.copy(),
                        "progress": f"{index + 1}/{variations}",
                        "session_id": session_id,
                    }
                    job_store.update(
                        job_id,
                        result=partial,
                        actual_cost=sum(item.get("cost", 0) for item in images),
                    )
                    if job_store.cancellation_requested(job_id):
                        refund(variations - len(images))
                        return {
                            "_job_status": "cancelled",
                            "images": images,
                            "session_id": session_id,
                            "message": f"Cancelled after {len(images)} image(s)",
                        }
                    with jobs_lock:
                        if not isinstance(jobs[job_id].get("result"), dict):
                            jobs[job_id]["result"] = {}
                        jobs[job_id]["result"]["images"] = images.copy()
                        jobs[job_id]["result"]["progress"] = (
                            f"{index + 1}/{variations}"
                        )
                persist_session_count()
                return {
                    "images": images,
                    "session_id": session_id,
                    "message": f"Generated {len(images)} image(s)",
                }

            run_job_background(job_id, generate_batch)
            response = {
                "job_id": job_id,
                "status": "running",
                "message": "Generation started",
                "idempotency_key": idempotency_key,
                "estimated_cost": round(estimated_cost, 4),
            }
            if used_trial_credit:
                response["credits_charged"] = credits_charged
            return jsonify(response)

        call_started = time.monotonic()
        images = run_generate(
            prompt, mode, api_key, tier, aspect, True, variations=variations
        )
        record_provider_results(
            owner_id, session_id, images,
            estimated_cost_each=estimate_cost(tier),
            latency_ms=(time.monotonic() - call_started) * 1000,
        )
        for image in images:
            if "error" not in image:
                record(
                    session_id, mode, prompt, aspect, image, owner_id,
                    parent_node_id,
                )
        delivered = sum(1 for image in images if "error" not in image)
        refund(variations - delivered)
        persist_session_count()
        payload = {
            "message": f"Generated {len(images)} image(s)",
            "images": images,
            "session_id": session_id,
        }
        if used_trial_credit:
            session = current_session()
            if session:
                payload.update(
                    {
                        "trial_credit_used": True,
                        "credits_charged": credits_per_image * delivered,
                        "credits_remaining": session["credits_remaining"],
                    }
                )
        return jsonify(payload)

    return blueprint
