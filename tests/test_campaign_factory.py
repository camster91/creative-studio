"""Campaign Factory contract: ownership, readiness, and safe generation plans."""

import importlib.util
import os
import sys
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).parent.parent / "scripts"


@pytest.fixture
def cs(tmp_path):
    web_path = SCRIPT_DIR / "creative-studio-web.py"
    sys.path.insert(0, str(SCRIPT_DIR))
    os.environ.setdefault("GEMINI_API_KEY", "test-key")
    spec = importlib.util.spec_from_file_location("creative_studio_web_campaigns", web_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.AUTH_DB = tmp_path / "users.db"
    module._init_auth_schema()
    with module._request_log_lock:
        module._request_log.clear()
    return module


def login(client, email="campaign@example.com"):
    token = client.post("/signup", json={"email": email}).get_json()["token"]
    return client.post("/login", json={"token": token}).get_json()["session_token"]


def headers(token):
    return {"X-Session-Token": token}


def complete_payload():
    return {
        "brand": {
            "name": "Northstar Nutrition",
            "voice": "Clear and energetic",
            "visual_rules": ["Use cobalt blue", "Keep the label unobstructed"],
            "forbidden_content": ["No medical imagery"],
        },
        "product": {
            "name": "Daily Hydration Mix",
            "sku": "HYD-BLU-12",
            "facts": ["12 single-serve sachets", "Blue raspberry flavour"],
            "approved_claims": ["Contains 6 essential electrolytes"],
            "required_disclosures": ["Always read and follow the label"],
        },
        "work_order": {
            "name": "Summer retail launch",
            "objective": "Drive retail trial",
            "audience": "Active adults, 25–44",
            "offer": "15% introductory offer",
            "channels": ["amazon", "meta-feed", "not-a-channel"],
            "creative_direction": "Bright summer tabletop with the package as hero",
            "aspect_ratio": "4:5",
            "tier": "quality",
            "variations": 4,
        },
    }


def test_page_exists(cs):
    response = cs.app.test_client().get("/campaigns")
    assert response.status_code == 200
    assert b"Campaign Factory" in response.data
    assert b'href="/signup"' in response.data


def test_anonymous_api_is_rejected(cs):
    assert cs.app.test_client().post("/api/campaigns", json={}).status_code == 401


def test_complete_work_order_is_ready_and_builds_bounded_plan(cs):
    client = cs.app.test_client()
    token = login(client)
    created = client.post("/api/campaigns", json=complete_payload(), headers=headers(token))
    assert created.status_code == 201
    body = created.get_json()
    assert body["readiness"]["ready"] is True
    assert body["channels"] == ["amazon", "meta-feed"]

    started = client.post(f"/api/campaigns/{body['id']}/go", headers=headers(token))
    assert started.status_code == 200
    plan = started.get_json()["generation_request"]
    assert plan["variations"] == 4
    assert plan["tier"] == "quality"
    assert plan["aspect_ratio"] == "4:5"
    assert "Contains 6 essential electrolytes" in plan["prompt"]
    assert "Do not invent packaging text" in plan["prompt"]
    assert "not-a-channel" not in plan["channels"]


def test_readiness_blocks_go_with_actionable_missing_fields(cs):
    client = cs.app.test_client()
    token = login(client)
    payload = complete_payload()
    payload["product"]["facts"] = []
    payload["work_order"]["channels"] = []
    created = client.post("/api/campaigns", json=payload, headers=headers(token)).get_json()
    assert created["readiness"]["ready"] is False
    fields = {item["field"] for item in created["readiness"]["missing"]}
    assert fields == {"product.facts", "work_order.channels"}

    blocked = client.post(f"/api/campaigns/{created['id']}/go", headers=headers(token))
    assert blocked.status_code == 409
    assert blocked.get_json()["readiness"]["ready"] is False


def test_campaigns_are_owner_scoped(cs):
    client = cs.app.test_client()
    owner = login(client, "owner@example.com")
    stranger = login(client, "stranger@example.com")
    campaign_id = client.post("/api/campaigns", json=complete_payload(), headers=headers(owner)).get_json()["id"]
    assert client.get(f"/api/campaigns/{campaign_id}", headers=headers(stranger)).status_code == 404
    assert client.post(f"/api/campaigns/{campaign_id}/go", headers=headers(stranger)).status_code == 404


def test_unapproved_claims_are_explicitly_prohibited(cs):
    client = cs.app.test_client()
    token = login(client)
    payload = complete_payload()
    payload["product"]["approved_claims"] = []
    campaign_id = client.post("/api/campaigns", json=payload, headers=headers(token)).get_json()["id"]
    plan = client.post(f"/api/campaigns/{campaign_id}/go", headers=headers(token)).get_json()["generation_request"]
    assert "Approved claims only: none; do not add marketing claims" in plan["prompt"]
