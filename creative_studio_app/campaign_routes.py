"""Guided Campaign Factory routes."""

from collections.abc import Callable

import hashlib

from flask import Blueprint, jsonify, render_template, request


def create_blueprint(*, current_session: Callable, create_campaign: Callable, list_campaigns: Callable,
                     list_brand_passports: Callable, list_product_truth: Callable,
                     get_campaign: Callable, campaign_readiness: Callable, build_generation_plan: Callable,
                     mark_campaign_started: Callable, attach_pack_asset: Callable,
                     save_upload: Callable, rate_limited: Callable) -> Blueprint:
    blueprint = Blueprint("campaigns", __name__)

    def signed_in():
        session = current_session()
        if not session:
            return None, (jsonify({"error": "Sign in required"}), 401)
        return session, None

    @blueprint.get("/campaigns")
    def campaign_factory():
        return render_template("campaigns.html")

    @blueprint.post("/api/campaigns")
    @rate_limited
    def create():
        session, error = signed_in()
        if error:
            return error
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "JSON object required"}), 400
        try:
            campaign = create_campaign(session["user_id"], payload)
        except LookupError:
            return jsonify({"error": "Reusable product truth not found"}), 404
        return jsonify({**campaign, "readiness": campaign_readiness(campaign)}), 201

    @blueprint.get("/api/brand-passports")
    @rate_limited
    def brands():
        session, error = signed_in()
        if error:
            return error
        return jsonify({"brand_passports": list_brand_passports(session["user_id"])})

    @blueprint.get("/api/product-truth")
    @rate_limited
    def products():
        session, error = signed_in()
        if error:
            return error
        brand_id = request.args.get("brand_id", "").strip()[:64] or None
        return jsonify({"products": list_product_truth(session["user_id"], brand_id)})

    @blueprint.get("/api/campaigns")
    @rate_limited
    def list_all():
        session, error = signed_in()
        if error:
            return error
        return jsonify({"campaigns": list_campaigns(session["user_id"])})

    @blueprint.get("/api/campaigns/<campaign_id>")
    @rate_limited
    def get(campaign_id):
        session, error = signed_in()
        if error:
            return error
        campaign = get_campaign(campaign_id, session["user_id"])
        if not campaign:
            return jsonify({"error": "Not found"}), 404
        return jsonify({**campaign, "readiness": campaign_readiness(campaign)})

    @blueprint.post("/api/campaigns/<campaign_id>/pack")
    @rate_limited
    def upload_pack(campaign_id):
        session, error = signed_in()
        if error:
            return error
        if not get_campaign(campaign_id, session["user_id"]):
            return jsonify({"error": "Not found"}), 404
        if "pack" not in request.files:
            return jsonify({"error": "Exact pack image required"}), 400
        try:
            saved = save_upload(request.files["pack"], "campaign-pack")
            digest = hashlib.sha256(saved.read_bytes()).hexdigest()
            campaign = attach_pack_asset(campaign_id, session["user_id"], saved.name, digest)
        except ValueError as upload_error:
            return jsonify({"error": str(upload_error)}), 400
        return jsonify({**campaign, "readiness": campaign_readiness(campaign)})

    @blueprint.post("/api/campaigns/<campaign_id>/go")
    @rate_limited
    def go(campaign_id):
        session, error = signed_in()
        if error:
            return error
        campaign = get_campaign(campaign_id, session["user_id"])
        if not campaign:
            return jsonify({"error": "Not found"}), 404
        gate = campaign_readiness(campaign)
        if not gate["ready"]:
            return jsonify({"error": "Campaign is not ready", "readiness": gate}), 409
        mark_campaign_started(campaign_id, session["user_id"], None)
        return jsonify({"campaign_id": campaign_id, "status": "ready", "generation_request": build_generation_plan(campaign)})

    return blueprint
