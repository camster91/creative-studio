"""Campaign Factory contract: ownership, readiness, and safe generation plans."""

import importlib.util
import io
import json
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
    module.SESSIONS_DIR = tmp_path / "sessions"
    module.OUTPUT_DIR = tmp_path / "outputs"
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


def register_owned_image(cs, token, relative="fixtures/concept.png", color=(20, 80, 190, 255)):
    source = cs.OUTPUT_DIR / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (800, 1200), color).save(source)
    user_id = cs._session_from_cookie(token)["user_id"]
    cs.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    (cs.SESSIONS_DIR / f"sess_{Path(relative).stem}.json").write_text(json.dumps({
        "id": f"sess_{Path(relative).stem}", "owner_id": f"user:{user_id}",
        "entries": [{"image_url": f"/image/{relative}"}],
    }))
    return f"/image/{relative}"


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
            "approved_claims": [{
                "text": "Contains 6 essential electrolytes",
                "claim_type": "nutrient",
                "markets": ["US"],
                "channels": ["amazon", "meta-feed"],
                "substantiation_url": "https://evidence.example/hydration-mix",
                "approval_reason": "Reviewed against the approved SKU substantiation package.",
                "required_disclosure": "Use only as directed.",
            }],
            "required_disclosures": ["Always read and follow the label"],
            "pack_asset_waived": True,
            "pack_asset_waiver_reason": "Packaging photography is pending; early art-direction concept only.",
        },
        "work_order": {
            "name": "Summer retail launch",
            "market": "US",
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
    assert "Use only as directed" in plan["prompt"]
    assert plan["market"] == "US"
    assert len(plan["approved_claim_ids"]) == 1
    assert "Do not invent packaging text" in plan["prompt"]
    assert "not-a-channel" not in plan["channels"]
    assert [(item["channel"], item["size"]) for item in plan["channel_deliverables"]] == [
        ("amazon", [2000, 2000]), ("meta-feed", [1080, 1350]),
    ]


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


def test_campaign_bundle_is_private_owner_scoped_and_marks_campaign_complete(cs):
    client = cs.app.test_client()
    token = login(client, "bundle-owner@example.com")
    stranger = login(client, "bundle-stranger@example.com")
    created = client.post("/api/campaigns", json=complete_payload(), headers=headers(token)).get_json()
    source = cs.OUTPUT_DIR / "fixtures" / "concept.png"
    source.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (800, 1200), (20, 80, 190, 255)).save(source)
    user_id = cs._session_from_cookie(token)["user_id"]
    cs.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    (cs.SESSIONS_DIR / "sess_deadbeef.json").write_text(json.dumps({
        "id": "sess_deadbeef", "owner_id": f"user:{user_id}",
        "entries": [{"image_url": "/image/fixtures/concept.png"}],
    }))
    response = client.post(
        f"/api/campaigns/{created['id']}/bundles",
        json={"image_urls": ["/image/fixtures/concept.png"], "session_id": "sess_deadbeef"},
        headers=headers(token),
    )
    assert response.status_code == 201
    bundle = response.get_json()
    assert bundle["manifest"]["deliverable_count"] == 2
    assert bundle["manifest"]["channels"] == ["amazon", "meta-feed"]
    assert bundle["manifest"]["publishing_status"] == "not_published"
    assert bundle["manifest"]["claim_evidence"][0]["exact_text"] == "Contains 6 essential electrolytes"
    assert bundle["manifest"]["claim_evidence"][0]["substantiation_url"].startswith("https://")
    assert client.get(bundle["download_url"], headers=headers(stranger)).status_code == 404
    download = client.get(bundle["download_url"], headers=headers(token))
    assert download.status_code == 200
    assert download.mimetype == "application/zip"
    listed = client.get(f"/api/campaigns/{created['id']}/bundles", headers=headers(token)).get_json()
    assert [item["id"] for item in listed["bundles"]] == [bundle["bundle_id"]]
    completed = client.get(f"/api/campaigns/{created['id']}", headers=headers(token)).get_json()
    assert completed["status"] == "completed"
    assert completed["last_session_id"] == "sess_deadbeef"


def test_campaign_bundle_rejects_unowned_images(cs):
    client = cs.app.test_client()
    token = login(client, "bundle-reject@example.com")
    created = client.post("/api/campaigns", json=complete_payload(), headers=headers(token)).get_json()
    response = client.post(
        f"/api/campaigns/{created['id']}/bundles",
        json={"image_urls": ["/image/another-owner.png"]}, headers=headers(token),
    )
    assert response.status_code == 404
    assert response.get_json()["error"] == "Campaign image not found"


def test_blocking_exception_requires_reasoned_approval_and_is_in_bundle_manifest(cs):
    client = cs.app.test_client()
    token = login(client, "exception-owner@example.com")
    campaign = client.post("/api/campaigns", json=complete_payload(), headers=headers(token)).get_json()
    image_url = register_owned_image(cs, token)
    created = client.post(
        f"/api/campaigns/{campaign['id']}/exceptions", headers=headers(token),
        json={"source": "claims", "criterion": "approved_claims", "severity": "blocking",
              "evidence": "Generated copy adds an unapproved efficacy claim.", "asset_url": image_url},
    )
    assert created.status_code == 201
    exception = created.get_json()
    duplicate = client.post(
        f"/api/campaigns/{campaign['id']}/exceptions", headers=headers(token),
        json={"source": "claims", "criterion": "approved_claims", "severity": "blocking",
              "evidence": "Generated copy adds an unapproved efficacy claim.", "asset_url": image_url},
    )
    assert duplicate.get_json()["id"] == exception["id"]
    assert len(client.get(
        f"/api/campaigns/{campaign['id']}/exceptions", headers=headers(token)
    ).get_json()["exceptions"]) == 1

    blocked = client.post(
        f"/api/campaigns/{campaign['id']}/bundles", headers=headers(token),
        json={"image_urls": [image_url]},
    )
    assert blocked.status_code == 409
    assert blocked.get_json()["exception_gate"]["blocking"][0]["id"] == exception["id"]
    assert client.post(
        f"/api/campaigns/{campaign['id']}/exceptions/{exception['id']}/resolve",
        headers=headers(token), json={"action": "approve", "reason": ""},
    ).status_code == 400
    approved = client.post(
        f"/api/campaigns/{campaign['id']}/exceptions/{exception['id']}/resolve",
        headers=headers(token), json={"action": "approve", "reason": "Legal verified the wording."},
    )
    assert approved.status_code == 200
    bundle = client.post(
        f"/api/campaigns/{campaign['id']}/bundles", headers=headers(token),
        json={"image_urls": [image_url]},
    )
    assert bundle.status_code == 201
    review = bundle.get_json()["manifest"]["exception_review"]
    assert review["allowed"] is True
    approved_review = next(item for item in review["exceptions"] if item["id"] == exception["id"])
    assert approved_review["status"] == "approved"
    assert approved_review["resolution_reason"] == "Legal verified the wording."


def test_rejected_and_repaired_original_assets_cannot_enter_bundle(cs):
    client = cs.app.test_client()
    token = login(client, "exception-repair@example.com")
    campaign = client.post("/api/campaigns", json=complete_payload(), headers=headers(token)).get_json()
    original = register_owned_image(cs, token, "fixtures/original.png")
    replacement = register_owned_image(cs, token, "fixtures/replacement.png", (40, 160, 80, 255))
    exception = client.post(
        f"/api/campaigns/{campaign['id']}/exceptions", headers=headers(token),
        json={"source": "channel", "criterion": "safe_area", "severity": "blocking",
              "evidence": "Disclosure is outside the safe area.", "asset_url": original},
    ).get_json()
    assert client.post(
        f"/api/campaigns/{campaign['id']}/exceptions/{exception['id']}/resolve",
        headers=headers(token), json={"action": "repair", "reason": "Repositioned disclosure."},
    ).status_code == 400
    assert client.post(
        f"/api/campaigns/{campaign['id']}/exceptions/{exception['id']}/resolve",
        headers=headers(token), json={"action": "repair", "reason": "Repositioned disclosure.",
                                     "replacement_url": replacement},
    ).status_code == 200
    blocked = client.post(
        f"/api/campaigns/{campaign['id']}/bundles", headers=headers(token),
        json={"image_urls": [original]},
    )
    assert blocked.status_code == 409
    assert blocked.get_json()["exception_gate"]["unusable"][0]["status"] == "repaired"
    assert client.post(
        f"/api/campaigns/{campaign['id']}/bundles", headers=headers(token),
        json={"image_urls": [replacement]},
    ).status_code == 201


def test_exception_inbox_is_private_and_qc_failures_are_recorded(cs):
    client = cs.app.test_client()
    owner = login(client, "exception-private@example.com")
    stranger = login(client, "exception-stranger@example.com")
    campaign = client.post("/api/campaigns", json=complete_payload(), headers=headers(owner)).get_json()
    assert client.get(f"/api/campaigns/{campaign['id']}/exceptions").status_code == 401
    assert client.get(
        f"/api/campaigns/{campaign['id']}/exceptions", headers=headers(stranger)
    ).status_code == 404
    user_id = cs._session_from_cookie(owner)["user_id"]
    recorded = cs._campaign_service.record_qc_exceptions(
        cs.AUTH_DB, campaign["id"], user_id,
        {"model": "qc-test", "rubric_version": "v1", "criteria": {
            "text_integrity": {"status": "fail", "evidence": "Label characters are malformed."},
            "physical_grounding": {"status": "fail", "evidence": "Shadow direction is inconsistent."},
            "brand_alignment": {"status": "pass", "evidence": "Palette matches."},
        }}, "/image/fixtures/concept.png",
    )
    assert [item["severity"] for item in recorded] == ["blocking", "warning"]
    assert {item["criterion"] for item in recorded} == {"text_integrity", "physical_grounding"}


def test_identical_exception_evidence_is_scoped_to_each_campaign(cs):
    client = cs.app.test_client()
    token = login(client, "exception-fingerprint@example.com")
    first = client.post("/api/campaigns", json=complete_payload(), headers=headers(token)).get_json()
    second_payload = complete_payload()
    second_payload["work_order"]["name"] = "Second campaign"
    second = client.post("/api/campaigns", json=second_payload, headers=headers(token)).get_json()
    finding = {"source": "human", "criterion": "brand_review", "severity": "warning",
               "evidence": "Confirm the final color treatment."}
    first_exception = client.post(
        f"/api/campaigns/{first['id']}/exceptions", headers=headers(token), json=finding,
    ).get_json()
    second_exception = client.post(
        f"/api/campaigns/{second['id']}/exceptions", headers=headers(token), json=finding,
    ).get_json()
    assert first_exception["id"] != second_exception["id"]


def test_qc_route_records_only_failed_criteria_for_owned_campaign_asset(cs, monkeypatch):
    client = cs.app.test_client()
    token = login(client, "exception-qc-route@example.com")
    campaign = client.post("/api/campaigns", json=complete_payload(), headers=headers(token)).get_json()
    image_url = register_owned_image(cs, token, "fixtures/qc-source.png")

    def fake_qc(*_args, **_kwargs):
        return {"quality_score": 6, "model": "qc-route-test", "rubric_version": "v1",
                "criteria": {
                    "label_readability": {"status": "fail", "evidence": "Required text is unreadable."},
                    "brand_alignment": {"status": "pass", "evidence": "Brand palette is correct."},
                }}

    monkeypatch.setattr(cs._delivery_service, "run_qc", fake_qc)
    response = client.post(
        "/api/qc", headers={**headers(token), "X-API-Key": "test-key"},
        json={"campaign_id": campaign["id"], "image_url": image_url},
    )
    assert response.status_code == 200
    exceptions = response.get_json()["exceptions"]
    assert len(exceptions) == 1
    assert exceptions[0]["criterion"] == "label_readability"
    assert exceptions[0]["severity"] == "blocking"


def test_bundle_preflight_routes_alpha_crop_loss_to_exception_inbox(cs):
    client = cs.app.test_client()
    token = login(client, "preflight-crop@example.com")
    payload = complete_payload()
    payload["work_order"]["channels"] = ["meta-story"]
    campaign = client.post("/api/campaigns", json=payload, headers=headers(token)).get_json()
    relative = "fixtures/edge-subject.png"
    source = cs.OUTPUT_DIR / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    canvas = Image.new("RGBA", (1200, 800), (0, 0, 0, 0))
    canvas.alpha_composite(Image.new("RGBA", (300, 600), (20, 80, 190, 255)), (0, 100))
    canvas.save(source)
    user_id = cs._session_from_cookie(token)["user_id"]
    cs.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    (cs.SESSIONS_DIR / "sess_edge_subject.json").write_text(json.dumps({
        "id": "sess_edge_subject", "owner_id": f"user:{user_id}",
        "entries": [{"image_url": f"/image/{relative}"}],
    }))
    blocked = client.post(
        f"/api/campaigns/{campaign['id']}/bundles", headers=headers(token),
        json={"image_urls": [f"/image/{relative}"]},
    )
    assert blocked.status_code == 409
    criteria = {item["criterion"] for item in blocked.get_json()["exception_gate"]["blocking"]}
    assert criteria == {"meta-story_subject_crop"}
    inbox = client.get(
        f"/api/campaigns/{campaign['id']}/exceptions", headers=headers(token),
    ).get_json()["exceptions"]
    assert {item["criterion"] for item in inbox} == {
        "meta-story_subject_crop", "claims_copy_unverified",
    }


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


def test_legacy_claim_strings_fail_readiness_until_evidence_is_registered(cs):
    client = cs.app.test_client()
    token = login(client, "legacy-claim@example.com")
    payload = complete_payload()
    payload["product"]["approved_claims"] = ["Supports all-day immunity"]
    created = client.post("/api/campaigns", json=payload, headers=headers(token))
    assert created.status_code == 201
    campaign = created.get_json()
    assert campaign["readiness"]["ready"] is False
    assert {item["field"] for item in campaign["readiness"]["missing"]} == {
        "product.approved_claim_evidence",
    }
    assert client.post(
        f"/api/campaigns/{campaign['id']}/go", headers=headers(token),
    ).status_code == 409


def test_claim_registry_is_owner_scoped_idempotent_and_retirable(cs):
    client = cs.app.test_client()
    owner = login(client, "claim-owner@example.com")
    stranger = login(client, "claim-stranger@example.com")
    campaign = client.post("/api/campaigns", json=complete_payload(), headers=headers(owner)).get_json()
    product_id = campaign["product_id"]
    claims = client.get(
        f"/api/product-truth/{product_id}/claims", headers=headers(owner),
    )
    assert claims.status_code == 200
    original = claims.get_json()["claims"][0]
    assert original["substantiation_url"].startswith("https://")
    assert client.get(
        f"/api/product-truth/{product_id}/claims", headers=headers(stranger),
    ).status_code == 404

    new_claim = {
        "text": "Made with recyclable packaging", "claim_type": "environmental",
        "markets": ["US"], "channels": ["amazon", "meta-feed"],
        "substantiation_url": "https://evidence.example/recyclability",
        "approval_reason": "Packaging specification and local acceptance were reviewed.",
    }
    first = client.post(
        f"/api/product-truth/{product_id}/claims", headers=headers(owner), json=new_claim,
    )
    duplicate = client.post(
        f"/api/product-truth/{product_id}/claims", headers=headers(owner), json=new_claim,
    )
    assert first.status_code == duplicate.status_code == 201
    assert first.get_json()["id"] == duplicate.get_json()["id"]
    assert client.post(
        f"/api/product-truth/{product_id}/claims/{first.get_json()['id']}/retire",
        headers=headers(owner), json={"reason": ""},
    ).status_code == 400
    retired = client.post(
        f"/api/product-truth/{product_id}/claims/{first.get_json()['id']}/retire",
        headers=headers(owner), json={"reason": "Supplier specification changed."},
    )
    assert retired.status_code == 200
    assert retired.get_json()["status"] == "retired"
    assert client.post(
        f"/api/product-truth/{product_id}/claims", headers=headers(owner), json=new_claim,
    ).status_code == 409
    assert client.post(
        f"/api/product-truth/{product_id}/claims/{original['id']}/retire",
        headers=headers(stranger), json={"reason": "Not mine"},
    ).status_code == 404


def test_claim_market_channel_and_expiry_scope_fail_closed(cs):
    client = cs.app.test_client()
    token = login(client, "claim-scope@example.com")
    payload = complete_payload()
    payload["work_order"]["market"] = "CA"
    created = client.post("/api/campaigns", json=payload, headers=headers(token)).get_json()
    assert created["readiness"]["ready"] is False
    assert created["ineligible_claims"][0]["exact_text"] == "Contains 6 essential electrolytes"

    expired_payload = complete_payload()
    expired_payload["product"]["approved_claims"][0]["expires_at"] = "2020-01-01"
    expired = client.post("/api/campaigns", json=expired_payload, headers=headers(token)).get_json()
    assert expired["readiness"]["ready"] is False
    assert {item["field"] for item in expired["readiness"]["missing"]} == {
        "product.approved_claim_evidence",
    }


def test_unapproved_claims_are_explicitly_prohibited(cs):
    client = cs.app.test_client()
    token = login(client)
    payload = complete_payload()
    payload["product"]["approved_claims"] = []
    campaign_id = client.post("/api/campaigns", json=payload, headers=headers(token)).get_json()["id"]
    plan = client.post(f"/api/campaigns/{campaign_id}/go", headers=headers(token)).get_json()["generation_request"]
    assert "Approved claims only: none; do not add marketing claims" in plan["prompt"]
