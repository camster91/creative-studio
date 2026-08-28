"""Composite, export, QC, and Figma Flask routes."""

import io
import re
import time
import urllib.request
import zipfile
from collections.abc import Callable

from flask import Blueprint, jsonify, request, send_file


def create_blueprint(
    *,
    safe_export_url: Callable[[str], bool],
    require_api_key: Callable,
    get_api_key: Callable[[], str],
    enforce_prompt_length: Callable,
    enforce_daily_limit: Callable,
    safe_filename: Callable[[str], str],
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
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for index, url in enumerate(urls, start=1):
                try:
                    probe = urllib.request.Request(
                        url, headers={"User-Agent": "CreativeStudio/1.0"}
                    )
                    with urllib.request.urlopen(probe, timeout=30) as response:
                        content_type = response.headers.get("Content-Type", "")
                        extension = ".jpg" if "jpeg" in content_type or "jpg" in content_type else ".webp" if "webp" in content_type else ".png"
                        archive.writestr(f"image-{index}{extension}", response.read())
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
        if "product" not in request.files:
            return jsonify({"error": "Product image required"}), 400
        prompt = request.form.get("prompt", "").strip()
        if not prompt:
            return jsonify({"error": "Prompt required"}), 400
        length_error = enforce_prompt_length(prompt)
        if length_error is not None:
            return length_error
        api_key, error, _used_trial_credit = require_api_key()
        if error is not None:
            return error
        tier = request.form.get("tier", "balanced")
        limit_error = enforce_daily_limit(1, tier)
        if limit_error is not None:
            return limit_error
        upload = request.files["product"]
        uploads = get_data_dir() / "uploads"
        uploads.mkdir(exist_ok=True)
        product = uploads / f"product_{int(time.time())}_{safe_filename(upload.filename)}"
        upload.save(str(product))
        aspect = request.form.get("aspect_ratio", "16:9")
        session_id = request.form.get("session_id", new_session_id())
        images = run_composite(prompt, str(product), api_key, aspect)
        for image in images:
            add_entry(
                session_id,
                {
                    "type": "composite",
                    "prompt": prompt[:100],
                    "cost": image.get("cost", 0),
                    "image_url": image.get("url", ""),
                    "model": image.get("model", ""),
                    "ratio": image.get("ratio", aspect),
                    "note": image.get("name", ""),
                },
            )
        return jsonify(
            {"message": "Composite generated", "images": images, "session_id": session_id}
        )

    @blueprint.post("/api/export")
    @rate_limited
    def export():
        presets = request.form.get("presets", "")
        if not presets:
            return jsonify({"error": "Presets required"}), 400
        image_url = request.form.get("image_url")
        if image_url and image_url.startswith("/image/"):
            source = safe_output_path(image_url[len("/image/") :])
            if not source:
                return jsonify(
                    {"error": "Image path is invalid or outside the output directory"}
                ), 400
        elif "image" in request.files:
            upload = request.files["image"]
            uploads = get_data_dir() / "uploads"
            uploads.mkdir(exist_ok=True)
            source = uploads / f"export_{int(time.time())}_{safe_filename(upload.filename)}"
            upload.save(str(source))
        else:
            return jsonify({"error": "Image required"}), 400
        session_id = request.form.get("session_id", new_session_id())
        images = run_export(str(source), presets, get_api_key())
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
        data = request.json or {}
        image_url = data.get("image_url", "")
        preset = data.get("preset", "")
        session_id = data.get("session_id") or None
        if session_id and image_url and preset:
            session = load_session(session_id)
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
        api_key, error, _used_trial_credit = require_api_key()
        if error is not None:
            return error
        data = request.json or request.form
        image_url = data.get("image_url")
        if image_url and image_url.startswith("/image/"):
            image = safe_output_path(image_url[len("/image/") :])
            if not image:
                return jsonify(
                    {"error": "Image path is invalid or outside the output directory"}
                ), 400
        elif "image" in request.files:
            upload = request.files["image"]
            uploads = get_data_dir() / "uploads"
            uploads.mkdir(exist_ok=True)
            image = uploads / f"qc_{int(time.time())}_{safe_filename(upload.filename)}"
            upload.save(str(image))
        else:
            return jsonify({"error": "Image required"}), 400
        result = run_qc(str(image), api_key)
        return jsonify(
            {"message": f"QC Score: {result['quality_score']}/10", "qc": result}
        )

    @blueprint.post("/api/figma")
    @rate_limited
    def figma():
        _api_key, error, _used_trial_credit = require_api_key()
        if error is not None:
            return error
        url = (request.json or {}).get("url")
        if not url:
            return jsonify({"error": "URL required"}), 400
        file_key, node_id = parse_figma_url(url)
        if not file_key:
            return jsonify({"error": "Invalid Figma URL"}), 400
        return jsonify(fetch_figma_context(file_key, node_id))

    return blueprint
