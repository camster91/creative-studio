"""Composite, export, QC, and Figma Flask routes."""

import io
import re
import time
import zipfile
from collections.abc import Callable

from flask import Blueprint, jsonify, request, send_file

from .billing import charge_for_images, unknown_tier_error


def create_blueprint(
    *,
    safe_export_url: Callable[[str], bool],
    current_session: Callable[[], dict | None],
    current_actor_id: Callable[[], str | None],
    owned_asset_paths: Callable[[str], set[str]],
    shared_figma_enabled: Callable[[], bool],
    require_api_key: Callable,
    require_access: Callable,
    get_api_key: Callable[[], str],
    enforce_prompt_length: Callable,
    enforce_daily_limit: Callable,
    safe_filename: Callable[[str], str],
    save_upload: Callable,
    safe_output_path: Callable[[str], object],
    get_data_dir: Callable,
    new_session_id: Callable[[], str],
    run_composite: Callable,
    run_export: Callable,
    run_qc: Callable,
    add_entry: Callable[[str, dict], None],
    load_session: Callable[[str], dict],
    save_session: Callable[[str, dict], None],
    parse_figma_url: Callable,
    fetch_figma_context: Callable,
    rate_limited: Callable,
    validate_version_parent: Callable = lambda *_args: True,
    current_version_node: Callable = lambda *_args: None,
    estimate_cost: Callable = lambda _tier: 0,
    record_provider_results: Callable = lambda *_args, **_kwargs: None,
    resolve_campaign_pack: Callable = lambda *_args: None,
    record_campaign_qc: Callable = lambda *_args: None,
    get_campaign: Callable = lambda *_args: None,
    refund_credits: Callable[[str, int], None] = lambda *_args: None,
) -> Blueprint:
    blueprint = Blueprint("delivery_routes", __name__)

    @blueprint.post("/api/export-zip")
    @rate_limited
    def export_zip():
        urls = (request.json or {}).get("urls", [])
        if not urls:
            return jsonify({"error": "No URLs provided"}), 400
        if not isinstance(urls, list) or len(urls) > 20:
            return jsonify({"error": "urls must be a list of at most 20 items"}), 400
        rejected = [url for url in urls if not safe_export_url(url)]
        if rejected:
            return jsonify(
                {
                    "error": "Rejected non-public or unsafe URL(s)",
                    "rejected": rejected[:5],
                }
            ), 400
        account = current_session()
        if not account:
            return jsonify({"error": "Sign in required"}), 401
        owned = owned_asset_paths(account["user_id"])
        unauthorized = [url for url in urls if url[len("/image/") :] not in owned]
        if unauthorized:
            return jsonify({"error": "Image not found"}), 404
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for index, url in enumerate(urls, start=1):
                try:
                    source = safe_output_path(url[len("/image/") :])
                    if not source:
                        raise ValueError("Image not found")
                    data = source.read_bytes()
                    if len(data) > 16 * 1024 * 1024:
                        raise ValueError("Image exceeds 16MB export limit")
                    extension = source.suffix.lower()
                    if extension not in {".jpg", ".jpeg", ".png", ".webp"}:
                        raise ValueError("Unsupported image type")
                    archive.writestr(f"image-{index}{extension}", data)
                except Exception as error:
                    archive.writestr(f"image-{index}-error.txt", str(error))
        buffer.seek(0)
        return send_file(
            buffer,
            mimetype="application/zip",
            as_attachment=True,
            download_name="creative-studio-export.zip",
        )

    @blueprint.post("/api/composite")
    @rate_limited
    def composite():
        account = current_session()
        campaign_id = request.form.get("campaign_id", "").strip()
        product = None
        if campaign_id:
            if not account:
                return jsonify({"error": "Sign in required"}), 401
            product = resolve_campaign_pack(campaign_id, account["user_id"])
            if not product:
                return jsonify({"error": "Campaign pack asset not found"}), 404
        elif "product" not in request.files:
            return jsonify({"error": "Product image or campaign pack required"}), 400
        prompt = request.form.get("prompt", "").strip()
        if not prompt:
            return jsonify({"error": "Prompt required"}), 400
        length_error = enforce_prompt_length(prompt)
        if length_error is not None:
            return length_error
        error = require_access()
        if error is not None:
            return error
        tier = request.form.get("tier", "balanced")
        tier_error = unknown_tier_error(tier)
        if tier_error is not None:
            return jsonify(tier_error), 400
        try:
            variations = min(8, max(1, int(request.form.get("variations", "1"))))
        except ValueError:
            return jsonify({"error": "Variations must be an integer from 1 to 8"}), 400
        limit_error = enforce_daily_limit(variations, tier)
        if limit_error is not None:
            return limit_error
        if product is None:
            try:
                product = save_upload(request.files["product"], "product")
            except ValueError as error:
                return jsonify({"error": str(error)}), 400
        aspect = request.form.get("aspect_ratio", "16:9")
        session_id = request.form.get("session_id", new_session_id())
        owner_id = current_actor_id()
        parent_node_id = request.form.get("parent_node_id") or None
        parent_node_id = parent_node_id or current_version_node(session_id, owner_id)
        if not validate_version_parent(session_id, owner_id, parent_node_id):
            return jsonify({"error": "Parent version not found"}), 404
        # Each variation is one composited image, charged at the request tier.
        api_key, error, refund = charge_for_images(
            require_api_key, current_session, refund_credits, tier, variations
        )
        if error is not None:
            return error
        call_started = time.monotonic()
        images = []
        for index in range(variations):
            images.extend(run_composite(prompt, str(product), api_key, aspect, tier=tier, name_suffix=f"{index + 1}-{new_session_id()[:6]}"))
        refund(variations - sum(1 for image in images if "error" not in image))
        record_provider_results(
            owner_id, session_id, images, estimated_cost_each=estimate_cost(tier),
            latency_ms=(time.monotonic() - call_started) * 1000,
        )
        for image in images:
            if "error" in image:
                continue
            node_id = add_entry(
                session_id,
                {
                    "type": "composite",
                    "prompt": prompt[:100],
                    "cost": image.get("cost", 0),
                    "image_url": image.get("url", ""),
                    "model": image.get("model", ""),
                    "ratio": image.get("ratio", aspect),
                    "note": image.get("name", ""),
                    "composite_manifest": image.get("composite_manifest"),
                    "parent_node_id": parent_node_id,
                },
                owner_id,
            )
            image["version_node_id"] = node_id
        return jsonify(
            {"message": "Composite generated", "images": images, "session_id": session_id}
        )

    @blueprint.post("/api/export")
    @rate_limited
    def export():
        account = current_session()
        if not account:
            return jsonify({"error": "Sign in required"}), 401
        presets = request.form.get("presets", "")
        if not presets:
            return jsonify({"error": "Presets required"}), 400
        image_url = request.form.get("image_url")
        if image_url and image_url.startswith("/image/"):
            if image_url[len("/image/") :] not in owned_asset_paths(account["user_id"]):
                return jsonify({"error": "Image not found"}), 404
            source = safe_output_path(image_url[len("/image/") :])
            if not source:
                return jsonify(
                    {"error": "Image path is invalid or outside the output directory"}
                ), 400
        elif "image" in request.files:
            try:
                source = save_upload(request.files["image"], "export")
            except ValueError as error:
                return jsonify({"error": str(error)}), 400
        else:
            return jsonify({"error": "Image required"}), 400
        session_id = request.form.get("session_id", new_session_id())
        images = run_export(str(source), presets, get_api_key())
        if images and "error" in images[0]:
            status = 400 if images[0].get("kind") == "validation" else 502
            return jsonify(images[0]), status
        selected = [preset.strip() for preset in presets.split(",") if preset.strip()]
        for image in images:
            add_entry(
                session_id,
                {
                    "type": "export",
                    "cost": 0,
                    "image_url": image.get("url", ""),
                    "model": "PIL",
                    "note": f"Exported to:[{', '.join(selected)}]",
                },
            )
        return jsonify(
            {
                "message": f"Exported to {len(images)} formats",
                "images": images,
                "session_id": session_id,
            }
        )

    @blueprint.post("/api/export-track")
    @rate_limited
    def export_track():
        if not current_session():
            return jsonify({"error": "Sign in required"}), 401
        data = request.json or {}
        image_url = data.get("image_url", "")
        preset = data.get("preset", "")
        session_id = data.get("session_id") or None
        if session_id and image_url and preset:
            session = load_session(session_id)
            if session.get("owner_id") != current_actor_id():
                return jsonify({"error": "Session not found"}), 404
            for entry in reversed(session.get("entries", [])):
                if entry.get("image_url") != image_url:
                    continue
                match = re.search(r"Exported to:\[(.*?)\]", entry.get("note", ""))
                used = [item.strip() for item in match.group(1).split(",")] if match else []
                if preset not in used:
                    used.append(preset)
                entry["note"] = f"Exported to:[{', '.join(used)}]"
                save_session(session_id, session)
                break
        return jsonify({"ok": True})

    @blueprint.post("/api/qc")
    @rate_limited
    def qc():
        data = request.get_json(silent=True) or request.form
        campaign_id = str(data.get("campaign_id") or "").strip()
        account = current_session() if campaign_id else None
        if campaign_id and not account:
            return jsonify({"error": "Sign in required"}), 401
        if campaign_id and not get_campaign(campaign_id, account["user_id"]):
            return jsonify({"error": "Campaign not found"}), 404
        access_error = require_access()
        if access_error is not None:
            return access_error
        image_url = data.get("image_url")
        if image_url and image_url.startswith("/image/"):
            if campaign_id and image_url[len("/image/"):] not in owned_asset_paths(account["user_id"]):
                return jsonify({"error": "Image not found"}), 404
            image = safe_output_path(image_url[len("/image/") :])
            if not image:
                return jsonify(
                    {"error": "Image path is invalid or outside the output directory"}
                ), 400
        elif "image" in request.files:
            try:
                image = save_upload(request.files["image"], "qc")
            except ValueError as error:
                return jsonify({"error": str(error)}), 400
        else:
            return jsonify({"error": "Image required"}), 400
        # QC costs one credit, spent only once the image is known to be valid.
        api_key, error, used_credit = require_api_key()
        if error is not None:
            return error
        actor_id = current_actor_id()
        call_started = time.monotonic()
        result = run_qc(str(image), api_key)
        if used_credit and result.get("error"):
            charged = current_session()
            if charged:
                refund_credits(charged["user_id"], 1)
        ledger_result = dict(result)
        ledger_result["cost"] = result.get("estimated_cost_usd") or 0
        ledger_result["model"] = result.get("model") or "qc-unknown"
        record_provider_results(
            actor_id, data.get("session_id") or "qc", [ledger_result],
            estimated_cost_each=result.get("estimated_cost_usd") or 0,
            latency_ms=(time.monotonic() - call_started) * 1000,
        )
        exceptions = []
        if campaign_id:
            exceptions = record_campaign_qc(campaign_id, account["user_id"], result, image_url)
            if exceptions is None:
                return jsonify({"error": "Campaign not found"}), 404
        return jsonify(
            {"message": f"QC Score: {result['quality_score']}/10", "qc": result,
             "exceptions": exceptions}
        )

    @blueprint.post("/api/figma")
    @rate_limited
    def figma():
        if not shared_figma_enabled():
            return jsonify({"error": "Shared Figma token access is disabled; configure per-user OAuth"}), 503
        # Fetching Figma context is not generation; never spend credits.
        error = require_access()
        if error is not None:
            return error
        url = (request.json or {}).get("url")
        if not url:
            return jsonify({"error": "URL required"}), 400
        file_key, node_id = parse_figma_url(url)
        if not file_key:
            return jsonify({"error": "Invalid Figma URL"}), 400
        actor_id = current_actor_id()
        call_started = time.monotonic()
        result = fetch_figma_context(file_key, node_id)
        ledger_result = {
            "error": result.get("error") if isinstance(result, dict) else "failed",
            "error_code": "provider_failed" if isinstance(result, dict) and result.get("error") else None,
            "model": "figma-rest-api",
        }
        record_provider_results(
            actor_id, "figma-context", [ledger_result], estimated_cost_each=0,
            latency_ms=(time.monotonic() - call_started) * 1000,
            provider="figma",
        )
        return jsonify(result)

    @blueprint.post("/api/qc/override")
    @rate_limited
    def qc_override():
        account = current_session()
        if not account:
            return jsonify({"error": "Sign in required"}), 401
        data = request.json or {}
        decision = data.get("decision")
        reason = str(data.get("reason") or "").strip()
        session_id = data.get("session_id")
        if decision not in {"accept", "reject"}:
            return jsonify({"error": "decision must be accept or reject"}), 400
        if not reason or len(reason.encode("utf-8")) > 1000:
            return jsonify({"error": "reason is required (max 1000 bytes)"}), 400
        if not session_id:
            return jsonify({"error": "session_id required"}), 400
        try:
            add_entry(session_id, {
                "type": "qc_override",
                "cost": 0,
                "image_url": str(data.get("image_url") or "")[:2000],
                "model": "human-review",
                "note": f"{decision}: {reason}",
                "qc_score": data.get("quality_score"),
            })
        except (ValueError, PermissionError):
            return jsonify({"error": "Session not found"}), 404
        return jsonify({"recorded": True, "decision": decision})

    return blueprint
