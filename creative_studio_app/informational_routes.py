"""Status, documentation, privacy, terms, and history Flask blueprint."""

from collections.abc import Callable

from flask import Blueprint, render_template

from .informational import render_docs, render_history, render_status


def create_blueprint(
    *,
    load_costs: Callable[[], dict],
    jobs: dict[str, dict],
    jobs_lock,
    get_sessions_dir: Callable,
    load_json: Callable,
    admin_authed: Callable[[], bool],
) -> Blueprint:
    blueprint = Blueprint("informational", __name__)

    @blueprint.get("/status")
    def status_page():
        if not admin_authed():
            return "Operator authentication required", 401
        with jobs_lock:
            snapshot = {identifier: dict(job) for identifier, job in jobs.items()}
        return render_status(load_costs(), snapshot)

    @blueprint.get("/docs")
    def docs_page():
        return render_docs()

    @blueprint.get("/privacy")
    def privacy_page():
        return render_template("privacy.html")

    @blueprint.get("/terms")
    def terms_page():
        return render_template("terms.html")

    @blueprint.get("/history")
    def history_page():
        if not admin_authed():
            return "Operator authentication required", 401
        return render_history(get_sessions_dir(), load_json)

    return blueprint
