"""Campaign Factory contract: ownership, readiness, and safe generation plans."""

import importlib.util
import io
import os
import sys
from pathlib import Path

import pytest
from PIL import Image


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
    module.DATA_DIR = tmp_path
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
            "pack_asset_waived": True,
            "pack_asset_waiver_reason": "Packaging photography is pending; early art-direction concept only.",
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
    assert cs.app.test_client().get("/api/brand-passports").status_code == 401
    assert cs.app.test_client().get("/api/product-truth").status_code == 401


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
    assert plan["execution_mode"] == "concept-only-direct"
    assert "Packaging fidelity is not guaranteed" in plan["fidelity_notice"]
    assert "Contains 6 essential electrolytes" in plan["prompt"]
    assert "Do not invent packaging text" in plan["prompt"]
    assert "not-a-channel" not in plan["channels"]


def png_upload():
    data = io.BytesIO()
    Image.new("RGBA", (64, 96), (20, 80, 190, 255)).save(data, "PNG")
    data.seek(0)
    return data


def test_exact_pack_is_owner_scoped_and_selects_composite_execution(cs):
    client = cs.app.test_client()
    owner = login(client, "pack-owner@example.com")
    stranger = login(client, "pack-stranger@example.com")
    payload = complete_payload()
    payload["product"]["pack_asset_waived"] = False
    payload["product"]["pack_asset_waiver_reason"] = ""
    created = client.post("/api/campaigns", json=payload, headers=headers(owner)).get_json()
    assert created["readiness"]["ready"] is False
    assert {item["field"] for item in created["readiness"]["missing"]} == {"product.pack_asset"}

    assert client.post(
        f"/api/campaigns/{created['id']}/pack",
        data={"pack": (png_upload(), "approved-pack.png")},
        headers=headers(stranger),
    ).status_code == 404
    attached = client.post(
        f"/api/campaigns/{created['id']}/pack",
        data={"pack": (png_upload(), "approved-pack.png")},
        headers=headers(owner),
    )
    assert attached.status_code == 200
    body = attached.get_json()
    assert body["has_pack_asset"] is True
    assert len(body["pack_asset_sha256"]) == 64
    assert body["readiness"]["ready"] is True

    plan = client.post(f"/api/campaigns/{created['id']}/go", headers=headers(owner)).get_json()["generation_request"]
    assert plan["mode"] == "composite"
    assert plan["execution_mode"] == "deterministic-pack-composite"
    assert plan["pack_asset_sha256"] == body["pack_asset_sha256"]
    assert "Generate an empty environment only" in plan["prompt"]
    assert "may alter edge pixels" in plan["fidelity_notice"]


def test_waiver_requires_a_reason(cs):
    client = cs.app.test_client()
    token = login(client)
    payload = complete_payload()
    payload["product"]["pack_asset_waiver_reason"] = ""
    created = client.post("/api/campaigns", json=payload, headers=headers(token)).get_json()
    assert created["readiness"]["ready"] is False
    assert {item["field"] for item in created["readiness"]["missing"]} == {"product.pack_asset_waiver_reason"}


def test_unusable_pack_is_rejected_before_attachment_and_cleaned_up(cs):
    client = cs.app.test_client()
    token = login(client, "tiny-pack@example.com")
    payload = complete_payload()
    payload["product"].update(pack_asset_waived=False, pack_asset_waiver_reason="")
    campaign_id = client.post("/api/campaigns", json=payload, headers=headers(token)).get_json()["id"]
    tiny = io.BytesIO()
    Image.new("RGB", (8, 8), (20, 80, 190)).save(tiny, "PNG")
    tiny.seek(0)
    rejected = client.post(
        f"/api/campaigns/{campaign_id}/pack",
        data={"pack": (tiny, "tiny.png")}, headers=headers(token),
    )
    assert rejected.status_code == 400
    assert "too small" in rejected.get_json()["error"]
    assert not list((cs.DATA_DIR / "uploads").glob("campaign-pack_*"))
    campaign = client.get(f"/api/campaigns/{campaign_id}", headers=headers(token)).get_json()
    assert campaign["has_pack_asset"] is False


def test_campaign_composite_uses_server_owned_pack_and_bounded_variations(cs, monkeypatch):
    client = cs.app.test_client()
    token = login(client, "composite@example.com")
    payload = complete_payload()
    payload["product"].update(pack_asset_waived=False, pack_asset_waiver_reason="")
    campaign_id = client.post("/api/campaigns", json=payload, headers=headers(token)).get_json()["id"]
    client.post(
        f"/api/campaigns/{campaign_id}/pack",
        data={"pack": (png_upload(), "approved-pack.png")},
        headers=headers(token),
    )
    calls = []

    def fake_composite(prompt, product_path, api_key, aspect, tier, **kwargs):
        calls.append((Path(product_path).name, aspect, tier, kwargs["name_suffix"]))
        return [{"url": f"/image/result-{len(calls)}.png", "name": f"result-{len(calls)}.png", "model": "test", "ratio": aspect}]

    monkeypatch.setattr(cs._generation_service, "composite", fake_composite)
    response = client.post(
        "/api/composite",
        data={"campaign_id": campaign_id, "prompt": "Clean summer tabletop", "aspect_ratio": "4:5", "tier": "quality", "variations": "3"},
        headers={**headers(token), "X-API-Key": "test-key"},
    )
    assert response.status_code == 200
    assert len(response.get_json()["images"]) == 3
    assert len(calls) == 3
    assert all(name.startswith("campaign-pack_") for name, _aspect, _tier, _suffix in calls)
    assert all(aspect == "4:5" and tier == "quality" for _name, aspect, tier, _suffix in calls)

    (cs.DATA_DIR / "uploads" / calls[0][0]).write_bytes(b"tampered")
    rejected = client.post(
        "/api/composite",
        data={"campaign_id": campaign_id, "prompt": "Clean summer tabletop"},
        headers={**headers(token), "X-API-Key": "test-key"},
    )
    assert rejected.status_code == 404
    assert rejected.get_json()["error"] == "Campaign pack asset not found"


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


def test_saved_brand_and_product_truth_are_reused_without_silent_rewrites(cs):
    client = cs.app.test_client()
    token = login(client, "reuse@example.com")
    first = client.post("/api/campaigns", json=complete_payload(), headers=headers(token)).get_json()
    reused_payload = complete_payload()
    reused_payload.update(brand_id=first["brand_id"], product_id=first["product_id"])
    reused_payload["brand"]["name"] = "Silently rewritten brand"
    reused_payload["product"]["sku"] = "SILENT-REWRITE"
    reused_payload["work_order"]["name"] = "Second work order"
    second = client.post("/api/campaigns", json=reused_payload, headers=headers(token))
    assert second.status_code == 201
    body = second.get_json()
    assert body["brand_id"] == first["brand_id"]
    assert body["product_id"] == first["product_id"]
    assert body["brand_name"] == "Northstar Nutrition"
    assert body["sku"] == "HYD-BLU-12"
    assert body["name"] == "Second work order"

    brands = client.get("/api/brand-passports", headers=headers(token)).get_json()["brand_passports"]
    products = client.get("/api/product-truth", headers=headers(token)).get_json()["products"]
    assert len(brands) == 1 and brands[0]["id"] == first["brand_id"]
    assert len(products) == 1 and products[0]["id"] == first["product_id"]
    assert products[0]["has_pack_asset"] is False


def test_reuse_is_owner_scoped_and_brand_filter_does_not_leak(cs):
    client = cs.app.test_client()
    owner = login(client, "truth-owner@example.com")
    stranger = login(client, "truth-stranger@example.com")
    created = client.post("/api/campaigns", json=complete_payload(), headers=headers(owner)).get_json()
    payload = complete_payload()
    payload.update(brand_id=created["brand_id"], product_id=created["product_id"])
    assert client.post("/api/campaigns", json=payload, headers=headers(stranger)).status_code == 404
    assert client.get("/api/brand-passports", headers=headers(stranger)).get_json()["brand_passports"] == []
    assert client.get(
        f"/api/product-truth?brand_id={created['brand_id']}", headers=headers(stranger)
    ).get_json()["products"] == []


def test_brand_can_be_reused_for_a_new_sku(cs):
    client = cs.app.test_client()
    token = login(client, "brand-reuse@example.com")
    first = client.post("/api/campaigns", json=complete_payload(), headers=headers(token)).get_json()
    payload = complete_payload()
    payload["brand_id"] = first["brand_id"]
    payload["product"]["name"] = "Recovery Mix"
    payload["product"]["sku"] = "REC-02"
    second = client.post("/api/campaigns", json=payload, headers=headers(token)).get_json()
    assert second["brand_id"] == first["brand_id"]
    assert second["product_id"] != first["product_id"]
    assert second["product_name"] == "Recovery Mix"
    assert len(client.get("/api/brand-passports", headers=headers(token)).get_json()["brand_passports"]) == 1
    assert len(client.get(
        f"/api/product-truth?brand_id={first['brand_id']}", headers=headers(token)
    ).get_json()["products"]) == 2


def test_unapproved_claims_are_explicitly_prohibited(cs):
    client = cs.app.test_client()
    token = login(client)
    payload = complete_payload()
    payload["product"]["approved_claims"] = []
    campaign_id = client.post("/api/campaigns", json=payload, headers=headers(token)).get_json()["id"]
    plan = client.post(f"/api/campaigns/{campaign_id}/go", headers=headers(token)).get_json()["generation_request"]
    assert "Approved claims only: none; do not add marketing claims" in plan["prompt"]
