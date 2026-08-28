"""Owner-scoped APIs for durable creative version graphs."""

import re
from collections.abc import Callable

from flask import Blueprint, jsonify


def create_blueprint(
    *,
    current_actor_id: Callable[[], str | None],
    graph_store,
    load_session: Callable[[str], dict],
    asset_exists: Callable[[str], bool],
    enabled: Callable[[], bool],
    rate_limited: Callable,
) -> Blueprint:
    blueprint = Blueprint("versions", __name__)

    def context(session_id):
        if not enabled():
            return None, (jsonify({"error": "Version history is disabled"}), 404)
        if not re.fullmatch(r"sess_[0-9a-f]{8}", session_id):
            return None, (jsonify({"error": "Invalid session id"}), 400)
        owner_id = current_actor_id()
        if not owner_id:
            return None, (jsonify({"error": "Sign in or provide an API key"}), 401)
        legacy = load_session(session_id)
        if graph_store.graph(session_id, owner_id) is None and legacy.get("owner_id") != owner_id:
            return None, (jsonify({"error": "Version graph not found"}), 404)
        graph_store.migrate_legacy(legacy, owner_id)
        return owner_id, None

    def response_graph(session_id, owner_id):
        graph = graph_store.graph(session_id, owner_id)
        if graph is None:
            return jsonify({"error": "Version graph not found"}), 404
        for node in graph["nodes"]:
            node["asset_available"] = bool(node.get("asset_url") and asset_exists(node["asset_url"]))
        return jsonify(graph)

    @blueprint.get("/api/session/<session_id>/versions")
    @rate_limited
    def get_graph(session_id):
        owner_id, error = context(session_id)
        return error or response_graph(session_id, owner_id)

    @blueprint.post("/api/session/<session_id>/versions/<node_id>/favorite")
    @rate_limited
    def favorite(session_id, node_id):
        owner_id, error = context(session_id)
        if error:
            return error
        try:
            graph_store.select(session_id, owner_id, node_id, favorite=True)
        except ValueError:
            return jsonify({"error": "Version not found"}), 404
        return response_graph(session_id, owner_id)

    @blueprint.post("/api/session/<session_id>/versions/<node_id>/branch")
    @rate_limited
    def branch(session_id, node_id):
        owner_id, error = context(session_id)
        if error:
            return error
        try:
            graph_store.select(session_id, owner_id, node_id)
        except ValueError:
            return jsonify({"error": "Version not found"}), 404
        return response_graph(session_id, owner_id)

    @blueprint.post("/api/session/<session_id>/versions/undo")
    @rate_limited
    def undo(session_id):
        owner_id, error = context(session_id)
        if error:
            return error
        try:
            graph_store.undo(session_id, owner_id)
        except ValueError:
            return jsonify({"error": "Version graph not found"}), 404
        return response_graph(session_id, owner_id)

    @blueprint.delete("/api/session/<session_id>/versions/<node_id>")
    @rate_limited
    def delete(session_id, node_id):
        owner_id, error = context(session_id)
        if error:
            return error
        try:
            graph_store.delete_branch(session_id, owner_id, node_id)
        except ValueError:
            return jsonify({"error": "Version not found"}), 404
        return response_graph(session_id, owner_id)

    return blueprint
