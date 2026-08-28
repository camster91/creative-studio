"""Frontend shell, job status, key validation, and image-serving routes."""

import urllib.error
import urllib.request
from collections.abc import Callable

from flask import Blueprint, jsonify, render_template, request, send_from_directory


def create_blueprint(
    *,
    landing_template: str,
    app_template: str,
    require_api_key: Callable,
    current_actor_id: Callable[[], str | None],
    job_store,
    jobs: dict,
    jobs_lock,
    get_output_dir: Callable,
    rate_limited: Callable,
) -> Blueprint:
    blueprint = Blueprint("core", __name__)

    @blueprint.get("/")
    def index():
        return render_template(landing_template)

    @blueprint.get("/app")
    def editor():
        return render_template(app_template)

    @blueprint.get("/api/jobs/<job_id>")
    @rate_limited
    def job_status(job_id):
        _key, auth_error, _used_trial = require_api_key()
        if auth_error:
            return auth_error
        actor_id = current_actor_id()
        if not actor_id:
            return jsonify({"error": "Sign in or provide an API key"}), 401
        job = job_store.get(job_id, actor_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        response = {
            "job_id": job_id,
            "status": job["status"],
            "started_at": job.get("started_at"),
            "estimated_cost": job.get("estimated_cost", 0),
            "actual_cost": job.get("actual_cost", 0),
        }
        if job["status"] == "completed" and job.get("result"):
            response.update(job["result"])
        elif job["status"] == "failed":
            response["error"] = "Generation job failed"
            response["error_code"] = job.get("error_code") or "job_failed"
            if job.get("result"):
                response["partial"] = job["result"]
        elif job["status"] == "cancelled":
            response["error"] = "Generation job cancelled"
            if job.get("result"):
                response["partial"] = job["result"]
        elif job.get("result"):
            response["partial"] = job["result"]
        return jsonify(response)

    @blueprint.post("/api/jobs/<job_id>/cancel")
    @rate_limited
    def cancel_job(job_id):
        _key, auth_error, _used_trial = require_api_key()
        if auth_error:
            return auth_error
        actor_id = current_actor_id()
        if not actor_id:
            return jsonify({"error": "Sign in or provide an API key"}), 401
        job = job_store.request_cancel(job_id, actor_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        return jsonify({"job_id": job_id, "status": job["status"]})

    @blueprint.post("/api/validate-key")
    @rate_limited
    def validate_key():
        key = ((request.json or {}).get("key") or "").strip()
        if not key:
            return jsonify({"error": "Key required"}), 400
        if not key.startswith("AIza"):
            return jsonify(
                {"error": "Invalid format — Gemini keys start with AIza..."}
            ), 400
        if len(key) > 200:
            return jsonify({"error": "Key too long"}), 400
        probe = urllib.request.Request(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"Content-Type": "application/json", "x-goog-api-key": key},
        )
        try:
            with urllib.request.urlopen(probe, timeout=10) as response:
                if response.status == 200:
                    return jsonify({"valid": True, "message": "Key is valid"})
        except urllib.error.HTTPError as error:
            if error.code in (400, 403):
                return jsonify({"valid": False, "error": "Invalid API key"}), 200
            return jsonify({"valid": False, "error": f"HTTP {error.code}"}), 200
        except Exception:
            return jsonify({"valid": False, "error": "Network error"}), 200
        return jsonify({"valid": True, "message": "Key looks valid"})

    @blueprint.get("/image/<path:subpath>")
    @rate_limited
    def image(subpath):
        parts = subpath.split("/")
        if any(part in ("", ".", "..") or part.startswith("..") for part in parts):
            return jsonify({"error": "Invalid path"}), 400
        output_dir = get_output_dir()
        target = output_dir.joinpath(*parts)
        try:
            resolved = target.resolve()
            resolved.relative_to(output_dir.resolve())
        except (ValueError, RuntimeError):
            return jsonify({"error": "Access denied"}), 403
        if not resolved.is_file():
            return jsonify({"error": "Not found"}), 404
        response = send_from_directory(
            str(resolved.parent),
            resolved.name,
            conditional=True,
            max_age=31536000,
        )
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    return blueprint
