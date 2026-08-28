"""Project CRUD and export Flask routes."""

import io
import json
import zipfile
from collections.abc import Callable

from flask import Blueprint, jsonify, request


def create_blueprint(
    *,
    current_session: Callable[[], dict | None],
    create_project: Callable,
    list_projects: Callable,
    get_project: Callable,
    delete_project: Callable,
    add_generation: Callable,
    safe_output_relpath: Callable,
    output_dir,
    rate_limited: Callable,
    project_name_max: int,
    generation_url_max: int,
    generation_prompt_max: int,
) -> Blueprint:
    blueprint = Blueprint("projects", __name__)

    def signed_in():
        session = current_session()
        if not session:
            return None, (jsonify({"error": "Sign in required"}), 401)
        return session, None

    @blueprint.post("/api/projects")
    @rate_limited
    def create():
        session, error = signed_in()
        if error:
            return error
        data = request.json or {}
        name = (data.get("name") or "Untitled project").strip()[:project_name_max]
        source = (data.get("source_session_id") or "").strip()[:64] or None
        return jsonify(create_project(session["user_id"], name, source)), 201

    @blueprint.get("/api/projects")
    @rate_limited
    def list_all():
        session, error = signed_in()
        if error:
            return error
        return jsonify({"projects": list_projects(session["user_id"])})

    @blueprint.get("/api/projects/<project_id>")
    @rate_limited
    def get(project_id):
        session, error = signed_in()
        if error:
            return error
        project = get_project(project_id, session["user_id"])
        if not project:
            return jsonify({"error": "Not found"}), 404
        return jsonify(project)

    @blueprint.delete("/api/projects/<project_id>")
    @rate_limited
    def delete(project_id):
        session, error = signed_in()
        if error:
            return error
        return jsonify({"deleted": delete_project(project_id, session["user_id"])})

    @blueprint.post("/api/projects/<project_id>/generations")
    @rate_limited
    def add(project_id):
        session, error = signed_in()
        if error:
            return error
        data = request.json or {}
        url = (data.get("url") or "").strip()[:generation_url_max]
        if not url:
            return jsonify({"error": "url required"}), 400
        project = add_generation(
            project_id,
            session["user_id"],
            url=url,
            prompt=(data.get("prompt") or "").strip()[:generation_prompt_max],
            cost=float(data.get("cost") or 0),
            model=str(data.get("model") or "")[:120],
            ratio=str(data.get("ratio") or "")[:16],
        )
        if not project:
            return jsonify({"error": "Not found"}), 404
        return jsonify(project)

    @blueprint.get("/api/projects/<project_id>/export")
    @rate_limited
    def export(project_id):
        session, error = signed_in()
        if error:
            return error
        project = get_project(project_id, session["user_id"])
        if not project:
            return jsonify({"error": "Not found"}), 404

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "manifest.json",
                json.dumps(
                    {
                        key: project[key]
                        for key in (
                            "id",
                            "name",
                            "hero_url",
                            "created_at",
                            "updated_at",
                            "generations",
                        )
                    },
                    indent=2,
                ),
            )
            for index, generation in enumerate(project["generations"], start=1):
                url = generation.get("url") or ""
                if not url.startswith("/image/"):
                    continue
                relative = safe_output_relpath(url[len("/image/") :])
                if relative is None:
                    continue
                image = output_dir / relative
                if image.is_file():
                    archive.write(str(image), f"images/{index}_{relative.name}")

        safe_name = (project["name"] or "project").strip().replace(" ", "_")
        safe_name = "".join(
            character
            for character in safe_name
            if character.isalnum() or character in "_-"
        )
        filename = f"photogen-{safe_name[:50]}-{project['id'][:8]}.zip"
        return buffer.getvalue(), 200, {
            "Content-Type": "application/zip",
            "Content-Disposition": f'attachment; filename="{filename}"',
        }

    return blueprint
