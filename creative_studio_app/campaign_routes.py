"""Guided Campaign Factory routes."""

from collections.abc import Callable

import hashlib

from flask import Blueprint, jsonify, render_template, request, send_file


def create_blueprint(*, current_session: Callable, create_campaign: Callable, list_campaigns: Callable,
                     list_brand_passports: Callable, list_product_truth: Callable,
                     get_campaign: Callable, campaign_readiness: Callable, build_generation_plan: Callable,
                     mark_campaign_started: Callable, attach_pack_asset: Callable,
                     save_upload: Callable, owned_asset_paths: Callable,
                     safe_output_path: Callable, build_campaign_bundle: Callable,
                     record_campaign_bundle: Callable, list_campaign_bundles: Callable,
                     get_campaign_bundle: Callable, campaign_bundle_path: Callable,
                     create_campaign_exception: Callable, list_campaign_exceptions: Callable,
                     resolve_campaign_exception: Callable, campaign_exception_gate: Callable,
                     record_campaign_preflight: Callable,
                     rate_limited: Callable) -> Blueprint:
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

    @blueprint.post("/api/campaigns/<campaign_id>/bundles")
    @rate_limited
    def create_bundle(campaign_id):
        session, error = signed_in()
        if error:
            return error
        campaign = get_campaign(campaign_id, session["user_id"])
        if not campaign:
            return jsonify({"error": "Not found"}), 404
        data = request.get_json(silent=True)
        image_urls = data.get("image_urls") if isinstance(data, dict) else None
        if not isinstance(image_urls, list) or not 1 <= len(image_urls) <= 8:
            return jsonify({"error": "image_urls must contain 1 to 8 campaign images"}), 400
        if any(not isinstance(url, str) or not url.startswith("/image/") for url in image_urls):
            return jsonify({"error": "Campaign images must use owned image URLs"}), 400
        if len(set(image_urls)) != len(image_urls):
            return jsonify({"error": "Campaign image URLs must be unique"}), 400
        owned = owned_asset_paths(session["user_id"])
        relative = [url[len("/image/"):] for url in image_urls]
        if any(item not in owned for item in relative):
            return jsonify({"error": "Campaign image not found"}), 404
        sources = []
        for url, item in zip(image_urls, relative):
            source = safe_output_path(item)
            if not source:
                return jsonify({"error": "Campaign image not found"}), 404
            sources.append((url, source))
        record_campaign_preflight(campaign_id, session["user_id"], campaign, sources)
        exception_gate = campaign_exception_gate(campaign_id, session["user_id"], image_urls)
        if exception_gate is None:
            return jsonify({"error": "Not found"}), 404
        if not exception_gate["allowed"]:
            return jsonify({
                "error": "Resolve blocking campaign exceptions before building this bundle",
                "exception_gate": exception_gate,
            }), 409
        try:
            bundle_campaign = {**campaign, "exception_summary": exception_gate}
            bundle_path, manifest = build_campaign_bundle(bundle_campaign, sources)
            bundle = record_campaign_bundle(
                campaign_id, session["user_id"], manifest, bundle_path,
                data.get("session_id") if isinstance(data, dict) else None,
            )
        except ValueError as bundle_error:
            return jsonify({"error": str(bundle_error)}), 400
        return jsonify({
            "bundle_id": bundle["id"],
            "manifest": bundle["manifest"],
            "download_url": f"/api/campaigns/{campaign_id}/bundles/{bundle['id']}/download",
        }), 201

    @blueprint.get("/api/campaigns/<campaign_id>/bundles")
    @rate_limited
    def bundles(campaign_id):
        session, error = signed_in()
        if error:
            return error
        if not get_campaign(campaign_id, session["user_id"]):
            return jsonify({"error": "Not found"}), 404
        return jsonify({"bundles": list_campaign_bundles(campaign_id, session["user_id"])})

    @blueprint.get("/api/campaigns/<campaign_id>/exceptions")
    @rate_limited
    def exceptions(campaign_id):
        session, error = signed_in()
        if error:
            return error
        if not get_campaign(campaign_id, session["user_id"]):
            return jsonify({"error": "Not found"}), 404
        return jsonify({"exceptions": list_campaign_exceptions(campaign_id, session["user_id"])})

    @blueprint.post("/api/campaigns/<campaign_id>/exceptions")
    @rate_limited
    def create_exception(campaign_id):
        session, error = signed_in()
        if error:
            return error
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "JSON object required"}), 400
        if data.get("source", "human") not in {"claims", "channel", "human"}:
            return jsonify({"error": "Manual exception source must be claims, channel, or human"}), 400
        asset_url = str(data.get("asset_url") or "").strip() or None
        if asset_url:
            if not asset_url.startswith("/image/"):
                return jsonify({"error": "Exception asset must use an owned image URL"}), 400
            if asset_url[len("/image/"):] not in owned_asset_paths(session["user_id"]):
                return jsonify({"error": "Campaign image not found"}), 404
        try:
            item = create_campaign_exception(
                campaign_id, session["user_id"], source=data.get("source", "human"),
                criterion=data.get("criterion", ""), severity=data.get("severity", ""),
                evidence=data.get("evidence", ""), asset_url=asset_url,
            )
        except ValueError as exception_error:
            return jsonify({"error": str(exception_error)}), 400
        if not item:
            return jsonify({"error": "Not found"}), 404
        return jsonify(item), 201

    @blueprint.post("/api/campaigns/<campaign_id>/exceptions/<exception_id>/resolve")
    @rate_limited
    def resolve_exception(campaign_id, exception_id):
        session, error = signed_in()
        if error:
            return error
        if not get_campaign(campaign_id, session["user_id"]):
            return jsonify({"error": "Not found"}), 404
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "JSON object required"}), 400
        replacement_url = str(data.get("replacement_url") or "").strip() or None
        if replacement_url:
            if not replacement_url.startswith("/image/"):
                return jsonify({"error": "Replacement must use an owned image URL"}), 400
            if replacement_url[len("/image/"):] not in owned_asset_paths(session["user_id"]):
                return jsonify({"error": "Replacement image not found"}), 404
        try:
            item = resolve_campaign_exception(
                exception_id, campaign_id, session["user_id"], action=data.get("action", ""),
                reason=data.get("reason", ""), replacement_url=replacement_url,
            )
        except ValueError as exception_error:
            return jsonify({"error": str(exception_error)}), 400
        if not item:
            return jsonify({"error": "Exception not found or already resolved"}), 409
        return jsonify(item)

    @blueprint.get("/api/campaigns/<campaign_id>/bundles/<bundle_id>/download")
    @rate_limited
    def download_bundle(campaign_id, bundle_id):
        session, error = signed_in()
        if error:
            return error
        bundle = get_campaign_bundle(bundle_id, campaign_id, session["user_id"])
        if not bundle:
            return jsonify({"error": "Not found"}), 404
        path = campaign_bundle_path(bundle)
        if not path:
            return jsonify({"error": "Bundle file not found"}), 404
        return send_file(path, mimetype="application/zip", as_attachment=True,
                         download_name=f"campaign-{campaign_id}-{bundle_id}.zip")

    return blueprint
