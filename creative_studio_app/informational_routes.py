"""Status, documentation, privacy, and history Flask blueprint."""

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
) -> Blueprint:
    blueprint = Blueprint("informational", __name__)

    @blueprint.get("/status")
    def status_page():
        with jobs_lock:
            snapshot = {identifier: dict(job) for identifier, job in jobs.items()}
        return render_status(load_costs(), snapshot)

    @blueprint.get("/docs")
    def docs_page():
        return render_docs()

    @blueprint.get("/privacy")
    def privacy_page():
        return render_template("privacy.html")

    @blueprint.get("/history")
    def history_page():
        return render_history(get_sessions_dir(), load_json)

    return blueprint
