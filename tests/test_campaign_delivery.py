import json
import zipfile
from pathlib import Path

from PIL import Image

from creative_studio_app.campaign_delivery import (
    CHANNEL_RECIPES,
    RECIPE_VERSION,
    build_bundle,
    channel_plan,
    preflight_findings,
)


def campaign(channels=None):
    return {
        "id": "campaign-1",
        "brand_id": "brand-1",
        "product_id": "product-1",
        "pack_asset_sha256": "a" * 64,
        "channels": channels or list(CHANNEL_RECIPES),
    }


def test_versioned_channel_plan_has_exact_dimensions():
    plan = channel_plan(list(CHANNEL_RECIPES))
    assert all(item["recipe_version"] == RECIPE_VERSION for item in plan)
    assert {item["channel"]: item["size"] for item in plan} == {
        "amazon": [2000, 2000], "shopify": [2048, 2048],
        "meta-feed": [1080, 1350], "meta-story": [1080, 1920],
        "pinterest": [1000, 1500], "email": [1200, 628], "web": [1920, 1080],
    }


def test_bundle_fans_every_concept_into_private_exact_outputs_and_manifest(tmp_path):
    first = tmp_path / "concept-a.png"
    second = tmp_path / "concept-b.png"
    Image.new("RGBA", (800, 1200), (20, 80, 190, 255)).save(first)
    Image.new("RGBA", (1200, 800), (220, 80, 30, 255)).save(second)
    selected = campaign(["amazon", "meta-story", "email"])
    path, manifest = build_bundle(
        selected, [("/image/a.png", first), ("/image/b.png", second)], tmp_path / "private",
    )
    assert path.is_file()
    assert manifest["publishing_status"] == "not_published"
    assert manifest["source_count"] == 2
    assert manifest["deliverable_count"] == 6
    assert manifest["pack_asset_sha256"] == "a" * 64
    assert len({item["sha256"] for item in manifest["deliverables"]}) >= 2
    for item in manifest["deliverables"]:
        assert [item["width"], item["height"]] == CHANNEL_RECIPES[item["channel"]]["size"]
        assert len(item["source_sha256"]) == 64
    with zipfile.ZipFile(path) as archive:
        assert set(archive.namelist()) == {
            "manifest.json",
            "variation-01/amazon.png", "variation-01/meta-story.png", "variation-01/email.png",
            "variation-02/amazon.png", "variation-02/meta-story.png", "variation-02/email.png",
        }
        assert json.loads(archive.read("manifest.json"))["bundle_id"] == manifest["bundle_id"]
    repeated_path, repeated_manifest = build_bundle(
        selected, [("/image/a.png", first), ("/image/b.png", second)], tmp_path / "private",
    )
    assert repeated_path == path
    assert repeated_manifest == manifest


def test_bundle_rejects_empty_or_unbounded_source_sets(tmp_path):
    for sources in ([], [("/image/x", Path("/tmp/x"))] * 9):
        try:
            build_bundle(campaign(["amazon"]), sources, tmp_path)
        except ValueError as error:
            assert "1 to 8" in str(error)
        else:
            raise AssertionError("Unbounded bundle sources were accepted")


def test_bundle_storage_limit_fails_closed_and_removes_partial_files(tmp_path):
    source = tmp_path / "concept.png"
    Image.new("RGB", (800, 1200), (20, 80, 190)).save(source)
    try:
        build_bundle(
            campaign(["amazon"]), [("/image/concept.png", source)],
            tmp_path / "private", max_bundle_bytes=1,
        )
    except ValueError as error:
        assert "storage limit" in str(error)
    else:
        raise AssertionError("Oversized bundle was accepted")
    assert not list((tmp_path / "private").rglob("*.png"))


def test_preflight_blocks_alpha_subject_crop_loss_and_warns_on_opaque_sources(tmp_path):
    transparent = tmp_path / "edge-subject.png"
    image = Image.new("RGBA", (1200, 800), (0, 0, 0, 0))
    subject = Image.new("RGBA", (300, 600), (20, 80, 190, 255))
    image.alpha_composite(subject, (0, 100))
    image.save(transparent)
    findings = preflight_findings(
        campaign(["meta-story"]), [("/image/edge-subject.png", transparent)],
    )
    assert findings == [{
        "criterion": "meta-story_subject_crop", "severity": "blocking",
        "evidence": "Visible alpha bounds (0, 100, 300, 700) extend outside the meta-story center-crop box (375, 0, 825, 800).",
        "asset_url": "/image/edge-subject.png",
    }]

    opaque = tmp_path / "opaque.png"
    Image.new("RGB", (800, 1200), (20, 80, 190)).save(opaque)
    warning = preflight_findings(campaign(["amazon"]), [("/image/opaque.png", opaque)])
    assert warning[0]["criterion"] == "subject_safe_area_unverified"
    assert warning[0]["severity"] == "warning"
