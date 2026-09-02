"""Owner-scoped CPG campaign inputs, deterministic readiness, and generation plans."""

import json
import hashlib
import secrets
from pathlib import Path

from .auth import connect, now_iso
from .campaign_delivery import channel_plan


ALLOWED_ASPECTS = frozenset({"1:1", "4:5", "9:16", "16:9", "2:3", "4:3"})
ALLOWED_TIERS = frozenset({"fast", "balanced", "quality", "ultra"})
ALLOWED_CHANNELS = frozenset({"amazon", "shopify", "meta-feed", "meta-story", "pinterest", "email", "web"})
EXCEPTION_SOURCES = frozenset({"qc", "claims", "channel", "human"})
EXCEPTION_SEVERITIES = frozenset({"warning", "blocking"})
EXCEPTION_ACTIONS = {"approve": "approved", "reject": "rejected", "repair": "repaired"}


def _clean(value, limit=2000):
    return str(value or "").strip()[:limit]


def _strings(value, *, limit=20, item_limit=500):
    if not isinstance(value, list):
        return []
    result = []
    for item in value[:limit]:
        cleaned = _clean(item, item_limit)
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result


def _json(raw):
    try:
        value = json.loads(raw or "[]")
        return value if isinstance(value, list) else []
    except (TypeError, ValueError):
        return []


def _serialize(row):
    if not row:
        return None
    result = dict(row)
    for key in tuple(result):
        if key.endswith("_json"):
            result[key[:-5]] = _json(result.pop(key))
    if "pack_asset_name" in result:
        result["has_pack_asset"] = bool(result.pop("pack_asset_name"))
    if "pack_asset_waived" in result:
        result["pack_asset_waived"] = bool(result["pack_asset_waived"])
    return result


def create_bundle(path: Path, user_id: str, payload: dict) -> dict:
    """Create a reusable brand, SKU truth record, and campaign work order atomically."""
    brand = payload.get("brand") if isinstance(payload.get("brand"), dict) else {}
    product = payload.get("product") if isinstance(payload.get("product"), dict) else {}
    work = payload.get("work_order") if isinstance(payload.get("work_order"), dict) else {}
    requested_brand_id = _clean(payload.get("brand_id"), 64)
    requested_product_id = _clean(payload.get("product_id"), 64)
    brand_id, product_id, campaign_id = (secrets.token_hex(16) for _ in range(3))
    now = now_iso()
    aspect = _clean(work.get("aspect_ratio"), 16)
    tier = _clean(work.get("tier"), 20)
    try:
        variations = int(work.get("variations", 4))
    except (TypeError, ValueError):
        variations = 4
    channels = [item for item in _strings(work.get("channels"), limit=8, item_limit=32) if item in ALLOWED_CHANNELS]
    with connect(path) as database:
        database.execute("BEGIN IMMEDIATE")
        if requested_product_id:
            existing_product = database.execute(
                """SELECT p.id,p.brand_id FROM product_truth p
                   JOIN brand_passports b ON b.id=p.brand_id AND b.user_id=p.user_id
                   WHERE p.id=? AND p.user_id=?""",
                (requested_product_id, user_id),
            ).fetchone()
            if not existing_product:
                database.rollback()
                raise LookupError("Reusable Product Truth not found")
            product_id = existing_product["id"]
            if requested_brand_id and existing_product["brand_id"] != requested_brand_id:
                database.rollback()
                raise LookupError("Reusable Product Truth not found")
            brand_id = existing_product["brand_id"]
        elif requested_brand_id:
            existing_brand = database.execute(
                "SELECT id FROM brand_passports WHERE id=? AND user_id=?",
                (requested_brand_id, user_id),
            ).fetchone()
            if not existing_brand:
                database.rollback()
                raise LookupError("Reusable Brand Passport not found")
            brand_id = existing_brand["id"]
        else:
            database.execute(
                """INSERT INTO brand_passports
                   (id,user_id,name,voice,visual_rules_json,forbidden_content_json,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (brand_id, user_id, _clean(brand.get("name"), 200), _clean(brand.get("voice"), 1000),
                 json.dumps(_strings(brand.get("visual_rules"))), json.dumps(_strings(brand.get("forbidden_content"))), now, now),
            )
        if not requested_product_id:
            database.execute(
                """INSERT INTO product_truth
                   (id,user_id,brand_id,name,sku,facts_json,approved_claims_json,required_disclosures_json,
                    pack_asset_waived,pack_asset_waiver_reason,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (product_id, user_id, brand_id, _clean(product.get("name"), 200), _clean(product.get("sku"), 120),
                 json.dumps(_strings(product.get("facts"))), json.dumps(_strings(product.get("approved_claims"))),
                 json.dumps(_strings(product.get("required_disclosures"))),
                 1 if product.get("pack_asset_waived") is True else 0,
                 _clean(product.get("pack_asset_waiver_reason"), 500), now, now),
            )
        database.execute(
            """INSERT INTO campaign_work_orders
               (id,user_id,brand_id,product_id,name,objective,audience,offer,channels_json,creative_direction,
                aspect_ratio,tier,variations,status,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'draft',?,?)""",
            (campaign_id, user_id, brand_id, product_id, _clean(work.get("name"), 200),
             _clean(work.get("objective"), 1000), _clean(work.get("audience"), 1000), _clean(work.get("offer"), 500),
             json.dumps(channels), _clean(work.get("creative_direction"), 2000),
             aspect if aspect in ALLOWED_ASPECTS else "1:1", tier if tier in ALLOWED_TIERS else "balanced",
             min(8, max(1, variations)), now, now),
        )
        database.commit()
    return get(path, campaign_id, user_id)


def list_brand_passports(path: Path, user_id: str) -> list[dict]:
    with connect(path) as database:
        rows = database.execute(
            """SELECT id,name,voice,visual_rules_json,forbidden_content_json,created_at,updated_at
               FROM brand_passports WHERE user_id=? ORDER BY updated_at DESC,id DESC LIMIT 100""",
            (user_id,),
        ).fetchall()
    return [_serialize(row) for row in rows]


def list_product_truth(path: Path, user_id: str, brand_id: str | None = None) -> list[dict]:
    parameters = [user_id]
    clause = ""
    if brand_id:
        clause = " AND p.brand_id=?"
        parameters.append(brand_id)
    with connect(path) as database:
        rows = database.execute(
            """SELECT p.id,p.brand_id,p.name,p.sku,p.facts_json,p.approved_claims_json,
                      p.required_disclosures_json,p.pack_asset_name,p.pack_asset_sha256,
                      p.pack_asset_waived,p.pack_asset_waiver_reason,p.created_at,p.updated_at,
                      b.name AS brand_name
               FROM product_truth p JOIN brand_passports b ON b.id=p.brand_id AND b.user_id=p.user_id
               WHERE p.user_id=?""" + clause + " ORDER BY p.updated_at DESC,p.id DESC LIMIT 100",
            parameters,
        ).fetchall()
    return [_serialize(row) for row in rows]


def get(path: Path, campaign_id: str, user_id: str) -> dict | None:
    with connect(path) as database:
        row = database.execute(
            """SELECT c.*, b.name AS brand_name, b.voice, b.visual_rules_json, b.forbidden_content_json,
                      p.name AS product_name, p.sku, p.facts_json, p.approved_claims_json, p.required_disclosures_json,
                      p.pack_asset_name, p.pack_asset_sha256, p.pack_asset_waived, p.pack_asset_waiver_reason
               FROM campaign_work_orders c
               JOIN brand_passports b ON b.id = c.brand_id AND b.user_id = c.user_id
               JOIN product_truth p ON p.id = c.product_id AND p.user_id = c.user_id
               WHERE c.id = ? AND c.user_id = ?""",
            (campaign_id, user_id),
        ).fetchone()
    return _serialize(row)


def list_for_user(path: Path, user_id: str) -> list[dict]:
    with connect(path) as database:
        rows = database.execute(
            """SELECT c.*, b.name AS brand_name, p.name AS product_name, p.sku
               FROM campaign_work_orders c
               JOIN brand_passports b ON b.id = c.brand_id
               JOIN product_truth p ON p.id = c.product_id
               WHERE c.user_id = ? ORDER BY c.updated_at DESC, c.id DESC LIMIT 100""",
            (user_id,),
        ).fetchall()
    return [_serialize(row) for row in rows]


def readiness(campaign: dict) -> dict:
    checks = [
        ("brand.name", campaign.get("brand_name"), "Add the brand name"),
        ("brand.visual_rules", campaign.get("visual_rules"), "Add at least one visual brand rule"),
        ("product.name", campaign.get("product_name"), "Add the product name"),
        ("product.sku", campaign.get("sku"), "Add an exact SKU or product identifier"),
        ("product.facts", campaign.get("facts"), "Add at least one verified product fact"),
        ("work_order.name", campaign.get("name"), "Name the campaign"),
        ("work_order.objective", campaign.get("objective"), "Choose a campaign objective"),
        ("work_order.audience", campaign.get("audience"), "Describe the target audience"),
        ("work_order.channels", campaign.get("channels"), "Choose at least one output channel"),
        ("work_order.creative_direction", campaign.get("creative_direction"), "Describe the creative direction"),
    ]
    missing = [{"field": field, "message": message} for field, value, message in checks if not value]
    if not campaign.get("has_pack_asset"):
        if not campaign.get("pack_asset_waived"):
            missing.append({"field": "product.pack_asset", "message": "Upload the exact pack asset or explicitly choose concept-only generation"})
        elif not campaign.get("pack_asset_waiver_reason"):
            missing.append({"field": "product.pack_asset_waiver_reason", "message": "Explain why this campaign is proceeding without an exact pack asset"})
    check_count = len(checks) + 1
    return {"ready": not missing, "missing": missing, "check_count": check_count, "passed_count": check_count - len(missing)}


def generation_plan(campaign: dict) -> dict:
    gate = readiness(campaign)
    if not gate["ready"]:
        raise ValueError("Campaign is not ready")
    claims = campaign.get("approved_claims") or []
    disclosures = campaign.get("required_disclosures") or []
    has_pack = campaign.get("has_pack_asset")
    prompt_parts = [
        f"Create a production-quality CPG campaign image for {campaign['brand_name']} {campaign['product_name']} (SKU {campaign['sku']}).",
        f"Campaign objective: {campaign['objective']}.",
        f"Audience: {campaign['audience']}.",
        f"Creative direction: {campaign['creative_direction']}.",
        "Verified product facts: " + "; ".join(campaign["facts"]) + ".",
        "Brand visual rules: " + "; ".join(campaign["visual_rules"]) + ".",
        ("Generate an empty environment only. Do not generate, redraw, restyle, obscure, or replace the product; the approved pack asset will be composited after generation."
         if has_pack else
         "CONCEPT ONLY: no exact pack asset was supplied. Do not imply packaging fidelity. Do not invent packaging text, ingredients, certifications, claims, prices, or product variants."),
    ]
    if campaign.get("voice"):
        prompt_parts.append(f"Brand voice: {campaign['voice']}.")
    if campaign.get("offer"):
        prompt_parts.append(f"Offer context: {campaign['offer']}.")
    prompt_parts.append("Approved claims only: " + ("; ".join(claims) if claims else "none; do not add marketing claims") + ".")
    if disclosures:
        prompt_parts.append("Required disclosures: " + "; ".join(disclosures) + ".")
    if campaign.get("forbidden_content"):
        prompt_parts.append("Forbidden content: " + "; ".join(campaign["forbidden_content"]) + ".")
    return {
        "prompt": " ".join(prompt_parts),
        "mode": "composite" if has_pack else "direct",
        "execution_mode": "deterministic-pack-composite" if has_pack else "concept-only-direct",
        "fidelity_notice": ("The validated pack source is composited after environment generation and its stored SHA-256 is recorded. Background cleanup and scaling may alter edge pixels."
                            if has_pack else
                            "Concept only. Packaging fidelity is not guaranteed because no exact pack asset was supplied."),
        "pack_asset_sha256": campaign.get("pack_asset_sha256") if has_pack else None,
        "tier": campaign["tier"],
        "aspect_ratio": campaign["aspect_ratio"],
        "variations": campaign["variations"],
        "channels": campaign["channels"],
        "channel_deliverables": channel_plan(campaign["channels"]),
        "campaign_id": campaign["id"],
    }


def attach_pack_asset(path: Path, campaign_id: str, user_id: str, asset_name: str, sha256: str) -> dict | None:
    """Attach a server-validated canonical upload to an owner-scoped product truth record."""
    if Path(asset_name).name != asset_name or not asset_name.endswith(".png"):
        raise ValueError("Invalid canonical pack asset")
    with connect(path) as database:
        updated = database.execute(
            """UPDATE product_truth SET pack_asset_name=?, pack_asset_sha256=?, pack_asset_waived=0,
                      pack_asset_waiver_reason='', updated_at=?
               WHERE id=(SELECT product_id FROM campaign_work_orders WHERE id=? AND user_id=?) AND user_id=?""",
            (asset_name, sha256, now_iso(), campaign_id, user_id, user_id),
        )
        database.commit()
    return get(path, campaign_id, user_id) if updated.rowcount else None


def internal_pack_asset(path: Path, campaign_id: str, user_id: str) -> tuple[str, str] | None:
    with connect(path) as database:
        row = database.execute(
            """SELECT p.pack_asset_name, p.pack_asset_sha256 FROM campaign_work_orders c
               JOIN product_truth p ON p.id=c.product_id AND p.user_id=c.user_id
               WHERE c.id=? AND c.user_id=?""", (campaign_id, user_id),
        ).fetchone()
    if not row or not row["pack_asset_name"] or not row["pack_asset_sha256"]:
        return None
    return row["pack_asset_name"], row["pack_asset_sha256"]


def mark_started(path: Path, campaign_id: str, user_id: str, session_id: str | None = None) -> None:
    with connect(path) as database:
        database.execute(
            "UPDATE campaign_work_orders SET status='started', last_session_id=?, updated_at=? WHERE id=? AND user_id=?",
            (_clean(session_id, 80) or None, now_iso(), campaign_id, user_id),
        )
        database.commit()


def record_bundle(path: Path, campaign_id: str, user_id: str, manifest: dict,
                  zip_relpath: str, session_id: str | None = None) -> dict | None:
    bundle_id = _clean(manifest.get("bundle_id"), 80)
    if not bundle_id or Path(zip_relpath).is_absolute() or ".." in Path(zip_relpath).parts:
        raise ValueError("Invalid campaign bundle record")
    with connect(path) as database:
        database.execute("BEGIN IMMEDIATE")
        campaign = database.execute(
            "SELECT id FROM campaign_work_orders WHERE id=? AND user_id=?",
            (campaign_id, user_id),
        ).fetchone()
        if not campaign:
            database.rollback()
            return None
        database.execute(
            """INSERT OR IGNORE INTO campaign_bundles
               (id,user_id,campaign_id,zip_relpath,manifest_json,source_session_id,created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (bundle_id, user_id, campaign_id, zip_relpath,
             json.dumps(manifest, separators=(",", ":"), sort_keys=True),
             _clean(session_id, 80) or None, now_iso()),
        )
        database.execute(
            """UPDATE campaign_work_orders SET status='completed',last_session_id=?,updated_at=?
               WHERE id=? AND user_id=?""",
            (_clean(session_id, 80) or None, now_iso(), campaign_id, user_id),
        )
        database.commit()
    return get_bundle(path, bundle_id, campaign_id, user_id)


def get_bundle(path: Path, bundle_id: str, campaign_id: str, user_id: str) -> dict | None:
    with connect(path) as database:
        row = database.execute(
            """SELECT id,campaign_id,zip_relpath,manifest_json,source_session_id,created_at
               FROM campaign_bundles WHERE id=? AND campaign_id=? AND user_id=?""",
            (bundle_id, campaign_id, user_id),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    result["manifest"] = json.loads(result.pop("manifest_json"))
    return result


def list_bundles(path: Path, campaign_id: str, user_id: str) -> list[dict]:
    with connect(path) as database:
        rows = database.execute(
            """SELECT id,campaign_id,manifest_json,source_session_id,created_at
               FROM campaign_bundles WHERE campaign_id=? AND user_id=?
               ORDER BY created_at DESC,id DESC LIMIT 50""",
            (campaign_id, user_id),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["manifest"] = json.loads(item.pop("manifest_json"))
        result.append(item)
    return result


def create_exception(path: Path, campaign_id: str, user_id: str, *, source: str,
                     criterion: str, severity: str, evidence: str,
                     asset_url: str | None = None, model: str | None = None,
                     rubric_version: str | None = None) -> dict | None:
    source = _clean(source, 32)
    criterion = _clean(criterion, 120)
    severity = _clean(severity, 20)
    evidence = _clean(evidence, 1000)
    asset_url = _clean(asset_url, 2000) or None
    model = _clean(model, 200) or None
    rubric_version = _clean(rubric_version, 100) or None
    if source not in EXCEPTION_SOURCES or severity not in EXCEPTION_SEVERITIES:
        raise ValueError("Invalid exception source or severity")
    if not criterion or not evidence:
        raise ValueError("Exception criterion and visible evidence are required")
    fingerprint = hashlib.sha256(json.dumps({
        "source": source, "criterion": criterion, "severity": severity,
        "evidence": evidence, "asset_url": asset_url, "model": model,
        "rubric_version": rubric_version,
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    identifier = "exc_" + hashlib.sha256(
        f"{user_id}:{campaign_id}:{fingerprint}".encode()
    ).hexdigest()[:20]
    with connect(path) as database:
        database.execute("BEGIN IMMEDIATE")
        campaign = database.execute(
            "SELECT id FROM campaign_work_orders WHERE id=? AND user_id=?",
            (campaign_id, user_id),
        ).fetchone()
        if not campaign:
            database.rollback()
            return None
        database.execute(
            """INSERT OR IGNORE INTO campaign_exceptions
               (id,user_id,campaign_id,fingerprint,source,criterion,severity,evidence,
                asset_url,model,rubric_version,status,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,'open',?)""",
            (identifier, user_id, campaign_id, fingerprint, source, criterion,
             severity, evidence, asset_url, model, rubric_version, now_iso()),
        )
        database.commit()
    return get_exception(path, identifier, campaign_id, user_id)


def get_exception(path: Path, exception_id: str, campaign_id: str, user_id: str) -> dict | None:
    with connect(path) as database:
        row = database.execute(
            """SELECT id,campaign_id,source,criterion,severity,evidence,asset_url,model,
                      rubric_version,status,resolution_reason,resolution_asset_url,
                      resolved_by,created_at,resolved_at
               FROM campaign_exceptions WHERE id=? AND campaign_id=? AND user_id=?""",
            (exception_id, campaign_id, user_id),
        ).fetchone()
    return dict(row) if row else None


def list_exceptions(path: Path, campaign_id: str, user_id: str) -> list[dict]:
    with connect(path) as database:
        rows = database.execute(
            """SELECT id,campaign_id,source,criterion,severity,evidence,asset_url,model,
                      rubric_version,status,resolution_reason,resolution_asset_url,
                      resolved_by,created_at,resolved_at
               FROM campaign_exceptions WHERE campaign_id=? AND user_id=?
               ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END,created_at DESC,id DESC LIMIT 200""",
            (campaign_id, user_id),
        ).fetchall()
    return [dict(row) for row in rows]


def resolve_exception(path: Path, exception_id: str, campaign_id: str, user_id: str,
                      *, action: str, reason: str, replacement_url: str | None = None) -> dict | None:
    status = EXCEPTION_ACTIONS.get(_clean(action, 20))
    reason = _clean(reason, 1000)
    replacement_url = _clean(replacement_url, 2000) or None
    if not status or not reason:
        raise ValueError("A valid action and review reason are required")
    if status == "repaired" and not replacement_url:
        raise ValueError("A repaired exception requires a replacement asset")
    with connect(path) as database:
        updated = database.execute(
            """UPDATE campaign_exceptions
               SET status=?,resolution_reason=?,resolution_asset_url=?,resolved_by=?,resolved_at=?
               WHERE id=? AND campaign_id=? AND user_id=? AND status='open'""",
            (status, reason, replacement_url, user_id, now_iso(),
             exception_id, campaign_id, user_id),
        )
        database.commit()
    if not updated.rowcount:
        return None
    return get_exception(path, exception_id, campaign_id, user_id)


def record_qc_exceptions(path: Path, campaign_id: str, user_id: str, assessment: dict,
                         asset_url: str | None = None) -> list[dict] | None:
    if not get(path, campaign_id, user_id):
        return None
    created = []
    blocking = {"text_integrity", "product_authenticity", "label_readability"}
    for criterion, result in (assessment.get("criteria") or {}).items():
        if not isinstance(result, dict) or result.get("status") != "fail":
            continue
        item = create_exception(
            path, campaign_id, user_id, source="qc", criterion=criterion,
            severity="blocking" if criterion in blocking else "warning",
            evidence=result.get("evidence") or "QC criterion failed without visible evidence.",
            asset_url=asset_url, model=assessment.get("model"),
            rubric_version=assessment.get("rubric_version"),
        )
        if item:
            created.append(item)
    return created


def bundle_exception_gate(path: Path, campaign_id: str, user_id: str,
                          image_urls: list[str]) -> dict | None:
    if not get(path, campaign_id, user_id):
        return None
    records = list_exceptions(path, campaign_id, user_id)
    applicable = [item for item in records if not item.get("asset_url") or item["asset_url"] in image_urls]
    blocking = [item for item in applicable if item["status"] == "open" and item["severity"] == "blocking"]
    unusable = [item for item in applicable if item["status"] in {"rejected", "repaired"} and item.get("asset_url") in image_urls]
    return {
        "allowed": not blocking and not unusable,
        "blocking": blocking,
        "unusable": unusable,
        "exceptions": applicable,
        "open_warning_count": sum(item["status"] == "open" and item["severity"] == "warning" for item in applicable),
    }
