"""Primary image-generation Flask route."""

from collections.abc import Callable

from flask import Blueprint, jsonify, request


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
    rate_limited: Callable,
) -> Blueprint:
    blueprint = Blueprint("generation_routes", __name__)

    def record(session_id: str, mode: str, prompt: str, aspect: str, image: dict):
        add_entry(
            session_id,
            {
                "type": mode,
                "prompt": prompt[:100],
                "cost": image.get("cost", 0),
                "image_url": image.get("url", ""),
                "model": image.get("model", ""),
                "ratio": image.get("ratio", aspect),
                "note": f"{image.get('name', '')} ({image.get('model', '')})",
            },
        )

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
        api_key, error, used_trial_credit = require_api_key()
        if error is not None:
            return error
        try:
            variations = int(data.get("variations", 1))
        except (TypeError, ValueError):
            return jsonify({"error": "variations must be an integer from 1 to 8"}), 400
        if not 1 <= variations <= 8:
            return jsonify({"error": "variations must be between 1 and 8"}), 400
        mode = data.get("mode", "direct")
        tier = data.get("tier", "balanced")
        aspect = data.get("aspect_ratio", "16:9")
        session_id = data.get("session_id", new_session_id())
        limit_error = enforce_daily_limit(variations, tier)
        if limit_error is not None:
            return limit_error
        figma_url = data.get("figma_url")
        if figma_url:
            file_key, node_id = parse_figma_url(figma_url)
            if file_key:
                context = fetch_figma_context(file_key, node_id)
                if "error" not in context:
                    prompt = enhance_prompt_with_figma(prompt, context)

        if variations > 1:
            job_id = new_job_id()

            def generate_batch():
                images = []
                for index in range(variations):
                    batch = run_generate(
                        prompt, mode, api_key, tier, aspect, True, variations=1
                    )
                    if not batch or "error" in batch[0]:
                        break
                    image = batch[0]
                    record(session_id, mode, prompt, aspect, image)
                    images.append(image)
                    with jobs_lock:
                        jobs[job_id].setdefault("result", {})
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
            return jsonify(
                {
                    "job_id": job_id,
                    "status": "running",
                    "message": "Generation started",
                }
            )

        images = run_generate(
            prompt, mode, api_key, tier, aspect, True, variations=1
        )
        for image in images:
            if "error" not in image:
                record(session_id, mode, prompt, aspect, image)
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
                        "credits_remaining": session["credits_remaining"],
                    }
                )
        return jsonify(payload)

    return blueprint
