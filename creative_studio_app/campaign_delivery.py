"""Versioned CPG channel recipes and private deterministic campaign bundles."""

import hashlib
import json
import os
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from .delivery import EXPORT_PRESETS, export_presets


RECIPE_VERSION = "cpg-channel-recipes-v1"
CHANNEL_RECIPES = {
    "amazon": {"preset": "amazon", "size": [2000, 2000], "ratio": "1:1", "background": "white", "dpi": 72},
    "shopify": {"preset": "shopify", "size": [2048, 2048], "ratio": "1:1", "background": "white", "dpi": 72},
    "meta-feed": {"preset": "meta-feed", "size": [1080, 1350], "ratio": "4:5", "background": "transparent", "dpi": 72},
    "meta-story": {"preset": "meta-stories", "size": [1080, 1920], "ratio": "9:16", "background": "transparent", "dpi": 72},
    "pinterest": {"preset": "pinterest", "size": [1000, 1500], "ratio": "2:3", "background": "transparent", "dpi": 72},
    "email": {"preset": "email", "size": [1200, 628], "ratio": "1200:628", "background": "transparent", "dpi": 72},
    "web": {"preset": "web-hero", "size": [1920, 1080], "ratio": "16:9", "background": "transparent", "dpi": 72},
}


def channel_plan(channels: list[str]) -> list[dict]:
    return [
        {"channel": channel, "recipe_version": RECIPE_VERSION, **CHANNEL_RECIPES[channel]}
        for channel in channels if channel in CHANNEL_RECIPES
    ]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _center_crop_box(width: int, height: int, ratio: str) -> tuple[int, int, int, int]:
    ratio_width, ratio_height = (int(value) for value in ratio.split(":"))
    target = ratio_width / ratio_height
    current = width / height
    if current > target:
        crop_width = round(height * target)
        left = (width - crop_width) // 2
        return left, 0, left + crop_width, height
    crop_height = round(width / target)
    top = (height - crop_height) // 2
    return 0, top, width, top + crop_height


def preflight_findings(campaign: dict, sources: list[tuple[str, Path]]) -> list[dict]:
    """Return deterministic source/crop findings without claiming visual understanding."""
    findings = []
    recipes = channel_plan(campaign.get("channels") or [])
    for source_url, source in sources:
        if campaign.get("approved_claims") or campaign.get("required_disclosures"):
            findings.append({
                "criterion": "claims_copy_unverified", "severity": "warning",
                "evidence": "This deterministic preflight does not OCR or legally validate rendered claims and disclosures; human copy review is required.",
                "asset_url": source_url,
            })
        with Image.open(source) as opened:
            image = opened.convert("RGBA")
            alpha = image.getchannel("A")
            extrema = alpha.getextrema()
            bbox = alpha.getbbox()
            if not bbox:
                findings.append({
                    "criterion": "non_empty_source", "severity": "blocking",
                    "evidence": "The source contains no visible alpha pixels.", "asset_url": source_url,
                })
                continue
            if extrema == (255, 255):
                findings.append({
                    "criterion": "subject_safe_area_unverified", "severity": "warning",
                    "evidence": "The source is fully opaque, so deterministic alpha bounds cannot verify subject crop safety.",
                    "asset_url": source_url,
                })
                continue
            for recipe in recipes:
                crop = _center_crop_box(image.width, image.height, recipe["ratio"])
                left, top, right, bottom = bbox
                crop_left, crop_top, crop_right, crop_bottom = crop
                outside = left < crop_left or top < crop_top or right > crop_right or bottom > crop_bottom
                criterion = f"{recipe['channel']}_subject_crop"
                if outside:
                    findings.append({
                        "criterion": criterion, "severity": "blocking",
                        "evidence": f"Visible alpha bounds {bbox} extend outside the {recipe['channel']} center-crop box {crop}.",
                        "asset_url": source_url,
                    })
                    continue
                margin_x = max(1, round((crop_right - crop_left) * 0.05))
                margin_y = max(1, round((crop_bottom - crop_top) * 0.05))
                if (left - crop_left < margin_x or crop_right - right < margin_x
                        or top - crop_top < margin_y or crop_bottom - bottom < margin_y):
                    findings.append({
                        "criterion": criterion, "severity": "warning",
                        "evidence": f"Visible alpha bounds {bbox} are within the 5% review margin of the {recipe['channel']} crop {crop}.",
                        "asset_url": source_url,
                    })
    return findings


def build_bundle(campaign: dict, sources: list[tuple[str, Path]], private_root: Path,
                 *, max_bundle_bytes: int = 256 * 1024 * 1024) -> tuple[Path, dict]:
    """Render every source through selected recipes and persist one private ZIP + manifest."""
    if not 1 <= len(sources) <= 8:
        raise ValueError("A campaign bundle requires 1 to 8 source images")
    recipes = channel_plan(campaign.get("channels") or [])
    if not recipes:
        raise ValueError("Campaign has no supported channel recipes")
    source_hashes = [_sha256(path) for _url, path in sources]
    fingerprint = hashlib.sha256(json.dumps({
        "campaign_id": campaign["id"],
        "recipe_version": RECIPE_VERSION,
        "channels": [recipe["channel"] for recipe in recipes],
        "sources": source_hashes,
        "exception_review": campaign.get("exception_summary") or {},
        "approved_claim_ids": [item["id"] for item in campaign.get("applicable_claims") or []],
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    bundle_id = f"bundle_{fingerprint[:20]}"
    bundle_dir = private_root / campaign["id"] / bundle_id
    bundle_path = bundle_dir / f"{bundle_id}.zip"
    manifest_path = bundle_dir / "manifest.json"
    if bundle_path.is_file() and manifest_path.is_file():
        return bundle_path, json.loads(manifest_path.read_text(encoding="utf-8"))

    bundle_dir.mkdir(parents=True, exist_ok=True)
    deliverables = []
    rendered = []
    total_bytes = 0
    try:
        for source_index, ((source_url, source), source_sha256) in enumerate(zip(sources, source_hashes), start=1):
            preset_names = ",".join(recipe["preset"] for recipe in recipes)
            outputs = export_presets(str(source), preset_names, bundle_dir / f"source-{source_index:02d}")
            for recipe, output in zip(recipes, outputs):
                archive_name = f"variation-{source_index:02d}/{recipe['channel']}.png"
                with Image.open(output) as image:
                    width, height = image.size
                if [width, height] != recipe["size"]:
                    raise ValueError(f"Rendered {recipe['channel']} dimensions do not match its recipe")
                total_bytes += output.stat().st_size
                if max_bundle_bytes < 1 or total_bytes > max_bundle_bytes:
                    raise ValueError("Campaign bundle exceeds the configured storage limit")
                rendered.append((output, archive_name))
                deliverables.append({
                    "variation": source_index,
                    "channel": recipe["channel"],
                    "recipe_version": RECIPE_VERSION,
                    "width": width,
                    "height": height,
                    "background": recipe["background"],
                    "dpi": recipe["dpi"],
                    "sha256": _sha256(output),
                    "source_sha256": source_sha256,
                    "source_url": source_url,
                    "archive_path": archive_name,
                })
    except Exception:
        shutil.rmtree(bundle_dir, ignore_errors=True)
        raise
    manifest = {
        "schema_version": 1,
        "bundle_id": bundle_id,
        "recipe_version": RECIPE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "campaign_id": campaign["id"],
        "brand_id": campaign["brand_id"],
        "product_id": campaign["product_id"],
        "pack_asset_sha256": campaign.get("pack_asset_sha256"),
        "channels": [recipe["channel"] for recipe in recipes],
        "source_count": len(sources),
        "deliverable_count": len(deliverables),
        "rendered_bytes": total_bytes,
        "deliverables": deliverables,
        "exception_review": campaign.get("exception_summary") or {
            "allowed": True, "blocking": [], "unusable": [], "exceptions": [],
            "open_warning_count": 0,
        },
        "claim_evidence": [{
            "id": item["id"], "exact_text": item["exact_text"],
            "claim_type": item["claim_type"], "markets": item["markets"],
            "channels": item["channels"], "substantiation_url": item["substantiation_url"],
            "required_disclosure": item.get("required_disclosure") or "",
            "approved_by": item["approved_by"], "approval_reason": item["approval_reason"],
            "approved_at": item["created_at"], "expires_at": item.get("expires_at"),
        } for item in campaign.get("applicable_claims") or []],
        "publishing_status": "not_published",
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    temporary = bundle_path.with_suffix(f".tmp.{os.getpid()}")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
        for output, archive_name in rendered:
            archive.write(output, archive_name)
    os.replace(temporary, bundle_path)
    return bundle_path, manifest
