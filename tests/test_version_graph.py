import threading

import pytest
from flask import Flask, request

from creative_studio_app.version_graph import VersionGraphStore
from creative_studio_app.version_routes import create_blueprint


def test_owner_scoped_branch_cost_favorite_undo_and_deletion(tmp_path):
    database = tmp_path / "versions.db"
    store = VersionGraphStore(database)
    root = store.add_node("sess_deadbeef", "user:a", operation="generate", asset_url="/image/a.png", cost=0.1)
    left = store.add_node("sess_deadbeef", "user:a", operation="variation", asset_url="/image/b.png", parent_id=root["id"], cost=0.2)
    right = store.add_node("sess_deadbeef", "user:a", operation="refine", asset_url="/image/c.png", parent_id=root["id"], cost=0.3)
    graph = store.select("sess_deadbeef", "user:a", right["id"], favorite=True)
    assert graph["cumulative_cost"] == 0.6
    assert graph["branch_cost"] == 0.4
    assert next(node for node in graph["nodes"] if node["id"] == right["id"])["favorite"]
    assert store.undo("sess_deadbeef", "user:a")["current_node_id"] == root["id"]
    graph = store.delete_branch("sess_deadbeef", "user:a", left["id"])
    assert {node["id"] for node in graph["nodes"]} == {root["id"], right["id"]}
    assert store.graph("sess_deadbeef", "user:b") is None
    assert VersionGraphStore(database).graph("sess_deadbeef", "user:a")["current_node_id"] == root["id"]
    with pytest.raises(ValueError, match="Parent"):
        store.add_node("sess_deadbeef", "user:b", operation="refine", asset_url=None, parent_id=root["id"])


def test_deletion_propagates_to_descendants_and_preserves_siblings(tmp_path):
    store = VersionGraphStore(tmp_path / "versions.db")
    root = store.add_node("sess_deadbeef", "owner", operation="generate", asset_url="/root")
    child = store.add_node("sess_deadbeef", "owner", operation="variation", asset_url="/child", parent_id=root["id"])
    grandchild = store.add_node("sess_deadbeef", "owner", operation="refine", asset_url="/grand", parent_id=child["id"])
    sibling = store.add_node("sess_deadbeef", "owner", operation="variation", asset_url="/sibling", parent_id=root["id"])
    store.select("sess_deadbeef", "owner", grandchild["id"])
    graph = store.delete_branch("sess_deadbeef", "owner", child["id"])
    assert {node["id"] for node in graph["nodes"]} == {root["id"], sibling["id"]}
    assert graph["current_node_id"] == root["id"]


def test_legacy_migration_is_idempotent_and_retains_missing_assets(tmp_path):
    store = VersionGraphStore(tmp_path / "versions.db")
    session = {
        "id": "sess_deadbeef",
        "owner_id": "owner",
        "entries": [
            {"type": "generate", "image_url": "/image/missing.png", "cost": 0.1},
            {"type": "chat", "note": "provider failed", "cost": 0},
        ],
    }
    assert store.migrate_legacy(session, "owner") == 2
    assert store.migrate_legacy(session, "owner") == 0
    graph = store.graph("sess_deadbeef", "owner")
    assert len(graph["nodes"]) == 2
    assert graph["nodes"][1]["status"] == "partial"
    assert session["entries"][0]["image_url"] == "/image/missing.png"
    assert store.migrate_legacy(session, "other") == 0


def test_concurrent_children_do_not_lose_branches(tmp_path):
    store = VersionGraphStore(tmp_path / "versions.db")
    root = store.add_node("sess_deadbeef", "owner", operation="generate", asset_url="/root")
    errors = []

    def create(index):
        try:
            store.add_node(
                "sess_deadbeef", "owner", operation="variation",
                asset_url=f"/image/{index}.png", parent_id=root["id"], cost=0.01,
            )
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=create, args=(index,)) for index in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    graph = store.graph("sess_deadbeef", "owner")
    assert len(graph["nodes"]) == 13
    assert graph["cumulative_cost"] == 0.12


def test_owner_scoped_routes_migrate_and_report_missing_assets(tmp_path):
    store = VersionGraphStore(tmp_path / "versions.db")
    sessions = {
        "sess_deadbeef": {
            "id": "sess_deadbeef",
            "owner_id": "key:owner-a",
            "entries": [
                {"type": "generate", "image_url": "/image/present.png", "cost": 0.1},
                {"type": "refine", "image_url": "/image/missing.png", "cost": 0.2},
            ],
        }
    }
    app = Flask(__name__)
    app.register_blueprint(create_blueprint(
        current_actor_id=lambda: (
            "key:" + request.headers["X-API-Key"]
            if request.headers.get("X-API-Key") else None
        ),
        graph_store=store,
        load_session=lambda identifier: sessions.get(identifier, {"id": identifier, "entries": []}),
        asset_exists=lambda url: url == "/image/present.png",
        enabled=lambda: True,
        rate_limited=lambda function: function,
    ))
    client = app.test_client()
    missing_auth = client.get("/api/session/sess_deadbeef/versions")
    assert missing_auth.status_code == 401
    graph = client.get(
        "/api/session/sess_deadbeef/versions", headers={"X-API-Key": "owner-a"}
    )
    assert graph.status_code == 200
    payload = graph.get_json()
    node_id = payload["nodes"][0]["id"]
    assert payload["nodes"][0]["asset_available"] is True
    assert payload["nodes"][1]["asset_available"] is False
    assert client.get(
        "/api/session/sess_deadbeef/versions", headers={"X-API-Key": "owner-b"}
    ).status_code == 404
    assert client.post(
        f"/api/session/sess_deadbeef/versions/{node_id}/favorite",
        headers={"X-API-Key": "owner-a"},
    ).get_json()["nodes"][0]["favorite"] is True
    assert client.delete(
        f"/api/session/sess_deadbeef/versions/{node_id}",
        headers={"X-API-Key": "owner-b"},
    ).status_code == 404
