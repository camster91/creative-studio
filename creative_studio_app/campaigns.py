"""Owner-scoped CPG campaign inputs, deterministic readiness, and generation plans."""

import json
import secrets
from pathlib import Path

from .auth import connect, now_iso


ALLOWED_ASPECTS = frozenset({"1:1", "4:5", "9:16", "16:9", "2:3", "4:3"})
ALLOWED_TIERS = frozenset({"fast", "balanced", "quality", "ultra"})
ALLOWED_CHANNELS = frozenset({"amazon", "shopify", "meta-feed", "meta-story", "pinterest", "email", "web"})


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
    return result


def create_bundle(path: Path, user_id: str, payload: dict) -> dict:
    """Create a reusable brand, SKU truth record, and campaign work order atomically."""
    brand = payload.get("brand") if isinstance(payload.get("brand"), dict) else {}
    product = payload.get("product") if isinstance(payload.get("product"), dict) else {}
    work = payload.get("work_order") if isinstance(payload.get("work_order"), dict) else {}
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
        database.execute(
            """INSERT INTO brand_passports
               (id,user_id,name,voice,visual_rules_json,forbidden_content_json,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (brand_id, user_id, _clean(brand.get("name"), 200), _clean(brand.get("voice"), 1000),
             json.dumps(_strings(brand.get("visual_rules"))), json.dumps(_strings(brand.get("forbidden_content"))), now, now),
        )
        database.execute(
            """INSERT INTO product_truth
               (id,user_id,brand_id,name,sku,facts_json,approved_claims_json,required_disclosures_json,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (product_id, user_id, brand_id, _clean(product.get("name"), 200), _clean(product.get("sku"), 120),
             json.dumps(_strings(product.get("facts"))), json.dumps(_strings(product.get("approved_claims"))),
             json.dumps(_strings(product.get("required_disclosures"))), now, now),
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


def get(path: Path, campaign_id: str, user_id: str) -> dict | None:
    with connect(path) as database:
        row = database.execute(
            """SELECT c.*, b.name AS brand_name, b.voice, b.visual_rules_json, b.forbidden_content_json,
                      p.name AS product_name, p.sku, p.facts_json, p.approved_claims_json, p.required_disclosures_json
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
    return {"ready": not missing, "missing": missing, "check_count": len(checks), "passed_count": len(checks) - len(missing)}


def generation_plan(campaign: dict) -> dict:
    gate = readiness(campaign)
    if not gate["ready"]:
        raise ValueError("Campaign is not ready")
    claims = campaign.get("approved_claims") or []
    disclosures = campaign.get("required_disclosures") or []
    prompt_parts = [
        f"Create a production-quality CPG campaign image for {campaign['brand_name']} {campaign['product_name']} (SKU {campaign['sku']}).",
        f"Campaign objective: {campaign['objective']}.",
        f"Audience: {campaign['audience']}.",
        f"Creative direction: {campaign['creative_direction']}.",
        "Verified product facts: " + "; ".join(campaign["facts"]) + ".",
        "Brand visual rules: " + "; ".join(campaign["visual_rules"]) + ".",
        "Do not invent packaging text, ingredients, certifications, claims, prices, or product variants.",
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
        "mode": "direct",
        "tier": campaign["tier"],
        "aspect_ratio": campaign["aspect_ratio"],
        "variations": campaign["variations"],
        "channels": campaign["channels"],
        "campaign_id": campaign["id"],
    }


def mark_started(path: Path, campaign_id: str, user_id: str, session_id: str | None = None) -> None:
    with connect(path) as database:
        database.execute(
            "UPDATE campaign_work_orders SET status='started', last_session_id=?, updated_at=? WHERE id=? AND user_id=?",
            (_clean(session_id, 80) or None, now_iso(), campaign_id, user_id),
        )
        database.commit()
