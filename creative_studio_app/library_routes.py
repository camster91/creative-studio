"""Generated-asset library Flask routes."""

import json
import re
from collections.abc import Callable

from flask import Blueprint, jsonify, request

MAX_RESULTS = 200
PAGE_SIZE = 60


def scan_output_dir(output_dir) -> list[dict]:
    if not output_dir.is_dir():
        return []
    items = []
    for pattern in ("*.png", "*.jpg"):
        for image in output_dir.rglob(pattern):
            try:
                stat = image.stat()
            except OSError:
                continue
            items.append(
                {
                    "path": str(image.relative_to(output_dir)),
                    "name": image.name,
                    "mtime": int(stat.st_mtime),
                    "size": stat.st_size,
                }
            )
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return items


def load_prompt(output_dir, path: str) -> str:
    image = output_dir / path
    sidecar = image.with_suffix(".json")
    if not sidecar.is_file():
        for suffix in (".json", ".prompt", ".txt"):
            candidate = image.parent / (image.stem + suffix)
            if candidate.is_file():
                sidecar = candidate
                break
        else:
            return ""
    try:
        data = json.loads(sidecar.read_text())
    except (OSError, ValueError):
        return ""
    if isinstance(data, dict):
        for key in ("prompt", "user_prompt", "original_prompt"):
            if data.get(key):
                return str(data[key])
    return ""


def create_blueprint(
    *,
    get_output_dir: Callable,
    current_session: Callable[[], dict | None],
    rate_limited: Callable,
) -> Blueprint:
    blueprint = Blueprint("library", __name__)

    @blueprint.get("/api/library")
    @rate_limited
    def library():
        if not current_session():
            return jsonify({"error": "Sign in required"}), 401
        output_dir = get_output_dir()
        search = (request.args.get("search") or "").strip().lower()
        aspect_query = (request.args.get("aspect") or "").strip()
        try:
            limit = min(int(request.args.get("limit", PAGE_SIZE)), MAX_RESULTS)
        except (TypeError, ValueError):
            limit = PAGE_SIZE
        try:
            offset = max(0, int(request.args.get("offset", 0)))
        except (TypeError, ValueError):
            offset = 0
        aspect_filename = aspect_query.replace(":", "_") if aspect_query else ""

        items = scan_output_dir(output_dir)
        if search or aspect_filename:
            filtered = []
            for item in items:
                if aspect_filename and aspect_filename not in item["path"]:
                    continue
                if search:
                    prompt = load_prompt(output_dir, item["path"])
                    if search not in prompt.lower() and search not in item["name"].lower():
                        continue
                filtered.append(item)
            items = filtered

        total = len(items)
        response_items = []
        for item in items[offset : offset + limit]:
            match = re.search(
                r"_(\d+)_(\d+)(?!_\d)", item["name"].rsplit(".", 1)[0]
            )
            response_items.append(
                {
                    **item,
                    "url": "/image/" + item["path"],
                    "prompt": load_prompt(output_dir, item["path"]),
                    "aspect": f"{match.group(1)}:{match.group(2)}" if match else "",
                }
            )
        return jsonify(
            {
                "items": response_items,
                "total": total,
                "limit": limit,
                "offset": offset,
            }
        )

    @blueprint.post("/api/library/<path:subpath>/delete")
    @rate_limited
    def delete(subpath):
        if not current_session():
            return jsonify({"error": "Sign in required"}), 401
        output_dir = get_output_dir()
        parts = [part for part in subpath.split("/") if part]
        if not parts or any(part in (".", "..") for part in parts):
            return jsonify({"error": "Invalid path"}), 400
        target = output_dir.joinpath(*parts)
        try:
            resolved = target.resolve()
            resolved.relative_to(output_dir.resolve())
        except (ValueError, RuntimeError):
            return jsonify({"error": "Invalid path"}), 400
        if not resolved.is_file():
            return jsonify({"error": "Not found"}), 404
        try:
            resolved.unlink()
        except OSError as error:
            return jsonify({"error": "Delete failed", "message": str(error)}), 500
        sidecar = resolved.with_suffix(".json")
        if sidecar.is_file():
            try:
                sidecar.unlink()
            except OSError:
                pass
        return jsonify({"deleted": True})

    return blueprint
