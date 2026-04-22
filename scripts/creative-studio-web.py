#!/usr/bin/env python3
# /// script
# requires-python: ">=3.10"
# dependencies = [
#     "flask>=3.0.0",
#     "pillow>=10.0.0",
#     "google-genai>=1.0.0",
# ]
# ///
"""
Creative Studio Web — Local creative design canvas with AI generation,
session management, annotation system, cost tracking, and version control.

Usage:
    uv run creative-studio-web.py [--port 5173]
    # Then open http://localhost:5173 in your browser
"""

import os
import sys
import json
import base64
import hashlib
import time
import uuid
import re
import subprocess
import platform
from pathlib import Path
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional

# ── Configuration ──────────────────────────────────────────────────────

API_KEY = os.environ.get("GEMINI_API_KEY")
API_BASE = "https://generativelanguage.googleapis.com/v1beta"

IMG_MODELS = {
    "nano-banana-2":  "gemini-3.1-flash-image-preview",
    "nano-banana-pro": "gemini-3-pro-image-preview",
    "nano-banana":    "gemini-2.5-flash-image",
}

IMAGEN_MODELS = {
    "imagen-4":       "models/imagen-4.0-generate-001",
    "imagen-4-fast":  "models/imagen-4.0-fast-generate-001",
    "imagen-4-ultra": "models/imagen-4.0-ultra-generate-001",
}

# Cost per image generation (USD)
MODEL_COSTS = {
    "imagen-4-fast":   0.02,
    "imagen-4":        0.04,
    "imagen-4-ultra":  0.06,
    "nano-banana-2":  0.07,
    "nano-banana-pro": 0.20,
}

FORMAT_SPECS = {
    "facebook-feed":     {"aspect": "1:1",    "resolution": "2K", "img_size": (1080, 1080), "type": "social"},
    "facebook-story":    {"aspect": "9:16",   "resolution": "2K", "img_size": (1080, 1920), "type": "social"},
    "instagram-feed":    {"aspect": "1:1",    "resolution": "2K", "img_size": (1080, 1080), "type": "social"},
    "instagram-story":   {"aspect": "9:16",   "resolution": "2K", "img_size": (1080, 1920), "type": "social"},
    "linkedin-feed":     {"aspect": "1.91:1", "resolution": "2K", "img_size": (1200, 627),  "type": "social"},
    "web-hero":          {"aspect": "16:9",   "resolution": "4K", "img_size": (1920, 1080), "type": "web"},
    "web-banner":        {"aspect": "3:1",    "resolution": "4K", "img_size": (2400, 800),  "type": "web"},
    "web-square":        {"aspect": "1:1",    "resolution": "4K", "img_size": (1600, 1600), "type": "web"},
    "web-portrait":      {"aspect": "3:4",    "resolution": "4K", "img_size": (1200, 1600), "type": "web"},
    "a4-portrait":       {"aspect": "3:4",    "resolution": "4K", "img_size": (2480, 3508), "type": "print"},
    "product-label-sq":  {"aspect": "1:1",    "resolution": "4K", "img_size": (1800, 1800), "type": "print"},
    "label-bottle":      {"aspect": "2:5",    "resolution": "4K", "img_size": (1200, 3000), "type": "print"},
    "label-jar":         {"aspect": "1:2",    "resolution": "4K", "img_size": (1400, 2800), "type": "print"},
    "hang-tag":          {"aspect": "2:3",    "resolution": "4K", "img_size": (1200, 1800), "type": "print"},
    "sticker-sq":        {"aspect": "1:1",    "resolution": "4K", "img_size": (1200, 1200), "type": "print"},
    "business-card":     {"aspect": "3.5:2",  "resolution": "4K", "img_size": (1050, 600),   "type": "print"},
    "postcard":          {"aspect": "6:4",    "resolution": "4K", "img_size": (1800, 1200), "type": "print"},
    "flyer-a5":          {"aspect": "3:4",    "resolution": "4K", "img_size": (1748, 2480), "type": "print"},
    "ppt-slide":         {"aspect": "16:9",   "resolution": "4K", "img_size": (1920, 1080), "type": "display"},
}

# ── Data Layer ──────────────────────────────────────────────────────────

DATA_DIR = Path.home() / "creative-studio-data"
SESSIONS_DIR = DATA_DIR / "sessions"
BRAND_DB = DATA_DIR / "brands.json"
COST_DB = DATA_DIR / "costs.json"
OUTPUT_DIR = Path.home() / "creative-studio-outputs"

DATA_DIR.mkdir(parents=True, exist_ok=True)
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def load_json(path: Path, default=None) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return default if default is not None else {}


def save_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def load_brands() -> dict:
    return load_json(BRAND_DB, {})


def save_brands(brands: dict):
    save_json(BRAND_DB, brands)


def load_costs() -> dict:
    return load_json(COST_DB, {"total": 0.0, "by_model": {}, "by_date": {}, "session_count": 0, "image_count": 0})


def save_costs(costs: dict):
    save_json(COST_DB, costs)


def track_cost(model: str, count: int = 1):
    costs = load_costs()
    c = MODEL_COSTS.get(model, 0.04) * count
    costs["total"] += c
    costs["by_model"][model] = costs["by_model"].get(model, 0.0) + c
    today = datetime.now().strftime("%Y-%m-%d")
    costs["by_date"][today] = costs["by_date"].get(today, 0.0) + c
    costs["image_count"] += count
    save_costs(costs)
    return c


def get_session_path(session_id: str) -> Path:
    return SESSIONS_DIR / f"{session_id}.json"


def load_session(session_id: str) -> dict:
    return load_json(get_session_path(session_id), {"id": session_id, "name": "", "created": datetime.now(timezone.utc).isoformat(), "entries": [], "total_cost": 0.0})


def save_session(session_id: str, data: dict):
    save_json(get_session_path(session_id), data)


def add_session_entry(session_id: str, entry: dict):
    session = load_session(session_id)
    entry["timestamp"] = datetime.now(timezone.utc).isoformat()
    entry["version"] = len(session["entries"]) + 1
    session["entries"].append(entry)
    session["total_cost"] = session["total_cost"] + entry.get("cost", 0)
    save_session(session_id, session)
    return entry["version"]


# ── Generation Engine ───────────────────────────────────────────────────

def gen_image(prompt: str, filename: Path, model: str = "nano-banana-2",
              resolution: str = "2K", input_image: str = None,
              api_key: str = None, negative_prompt: str = None) -> Optional[str]:
    from google import genai
    from google.genai import types
    from PIL import Image as PILImage

    client = genai.Client(api_key=api_key or API_KEY)
    model_id = IMG_MODELS.get(model, IMG_MODELS["nano-banana-2"])

    input_pil = None
    if input_image:
        input_pil = PILImage.open(input_image)
    contents = [input_pil, prompt] if input_pil else prompt

    try:
        response = client.models.generate_content(
            model=model_id,
            contents=contents,
            config=types.GenerateContentConfig(
                response_modalities=["TEXT", "IMAGE"],
                image_config=types.ImageConfig(image_size=resolution),
            )
        )
        for part in response.parts:
            if part.inline_data is not None:
                data = part.inline_data.data
                if isinstance(data, str):
                    data = base64.b64decode(data)
                img = PILImage.open(BytesIO(data))
                if img.mode == "RGBA":
                    rgb = PILImage.new("RGB", img.size, (255, 255, 255))
                    rgb.paste(img, mask=img.split()[3])
                    img = rgb
                elif img.mode != "RGB":
                    img = img.convert("RGB")
                img.save(str(filename), "PNG")
                return str(filename)
    except Exception as e:
        print(f"[ERR] gen_image: {e}", file=sys.stderr)
    return None


def gen_imagen(prompt: str, filename: Path, model: str = "imagen-4-fast",
               aspect_ratio: str = "1:1", api_key: str = None) -> Optional[str]:
    import urllib.request, urllib.error
    key = api_key or API_KEY
    model_id = IMAGEN_MODELS.get(model, IMAGEN_MODELS["imagen-4-fast"])
    url = f"{API_BASE}/models/{model_id}:predict?key={key}"
    params = {"instances": [{"prompt": prompt}], "parameters": {"aspectRatio": aspect_ratio, "sampleCount": 1}}

    try:
        req = urllib.request.Request(url, data=json.dumps(params).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
        if "predictions" in result:
            pred = result["predictions"][0]
            if "bytesBase64Encoded" in pred:
                data = base64.b64decode(pred["bytesBase64Encoded"])
                filename.parent.mkdir(parents=True, exist_ok=True)
                with open(str(filename), "wb") as f:
                    f.write(data)
                return str(filename)
    except Exception as e:
        print(f"[ERR] gen_imagen: {e}", file=sys.stderr)
    return None


# ── Prompt Builder ──────────────────────────────────────────────────────

def build_prompt(task_type: str, raw: str, format_name: str, brand: dict = None, annotations: list = None) -> str:
    fspec = FORMAT_SPECS.get(format_name, FORMAT_SPECS["facebook-feed"])
    parts = []

    if fspec["type"] == "print":
        parts.append(f"Professional print design. {raw}")
        parts.append(f"Format: {format_name.replace('-', ' ')}. High resolution.")
    elif fspec["type"] == "social":
        parts.append(f"Professional social media advertisement. {raw}")
        parts.append(f"Format: {format_name.replace('-', ' ')}. Clean modern marketing style.")
    elif fspec["type"] == "web":
        parts.append(f"Professional web design asset. {raw}")
        parts.append(f"Format: {format_name.replace('-', ' ')}. Clean UI.")
    else:
        parts.append(raw)

    if brand:
        if brand.get("style_summary"):
            parts.append(f"Style: {brand['style_summary']}")
        if brand.get("colors"):
            parts.append(f"Color palette: {', '.join(brand['colors'])}")
        if brand.get("rules"):
            parts.append(f"Brand rules: {'; '.join(brand['rules'])}")

    if annotations:
        for a in annotations:
            parts.append(f"Note: at approximately x={a['x']:.0f}%, y={a['y']:.0f}% — {a['comment']}")

    # Add text quality instruction
    if any(w in raw.lower() for w in ["text", "word", "headline", "title", "logo", "font"]):
        parts.append("If text appears in the image, ensure it is spelled correctly, legible, and crisp.")

    return ". ".join(parts)


# ── Model Intelligence Router ──────────────────────────────────────────

def infer_model(prompt: str, format_name: str, preferred: str = None, budget: str = "balanced") -> str:
    if preferred and preferred in MODEL_COSTS:
        return preferred

    if budget == "cheap":
        return "imagen-4-fast"
    if budget == "quality":
        return "nano-banana-pro" if any(w in prompt.lower() for w in ["complex", "composition", "multi", "references"]) else "imagen-4-ultra"

    p = prompt.lower()
    if any(w in p for w in ["edit", "change", "revise", "fix", "make it", "darker", "brighter"]):
        return "nano-banana-2"
    if any(w in p for w in ["product", "clean", "simple", "single"]):
        return "imagen-4"
    if fspec := FORMAT_SPECS.get(format_name):
        if fspec.get("type") == "print":
            return "imagen-4-ultra"
    return "imagen-4-fast"


# ── Flask App ────────────────────────────────────────────────────────────

from flask import Flask, render_template_string, request, jsonify, send_from_directory

app = Flask(__name__)

HTML = """""


# ── Routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/generate", methods=["POST"])
def api_generate():
    body = request.json or {}
    prompt = body.get("prompt", "").strip()
    if not prompt:
        return jsonify({"error": "Prompt required"}), 400

    session_id = body.get("session_id")
    if not session_id:
        return jsonify({"error": "session_id required — create a session first"}), 400

    format_name = body.get("format", "facebook-feed")
    model = body.get("model")
    budget = body.get("budget", "balanced")
    brand_id = body.get("brand_id")
    annotations = body.get("annotations", [])
    input_image = body.get("input_image")

    inferred_model = infer_model(prompt, format_name, model, budget)
    cost = MODEL_COSTS.get(inferred_model, 0.04)

    fspec = FORMAT_SPECS.get(format_name, FORMAT_SPECS["facebook-feed"])

    # Apply brand
    brands = load_brands()
    brand = brands.get(brand_id) if brand_id else None
    full_prompt = build_prompt("generate", prompt, format_name, brand, annotations)

    # Build file path
    ts = datetime.now().strftime("%H%M%S")
    hash5 = hashlib.md5(full_prompt[:80].encode()).hexdigest()[:5]
    outdir = OUTPUT_DIR / datetime.now().strftime("%Y-%m-%d") / format_name
    outdir.mkdir(parents=True, exist_ok=True)
    filename = f"{ts}-{format_name}-{hash5}.png"
    outpath = outdir / filename

    result = None
    if inferred_model.startswith("imagen"):
        result = gen_imagen(full_prompt, outpath, model=inferred_model, aspect_ratio=fspec["aspect"])
    else:
        result = gen_image(full_prompt, outpath, model=inferred_model, resolution=fspec["resolution"], input_image=input_image)

    if result:
        track_cost(inferred_model)
        version = add_session_entry(session_id, {
            "type": "generate",
            "prompt": prompt,
            "full_prompt": full_prompt,
            "model": inferred_model,
            "format": format_name,
            "cost": cost,
            "annotations": annotations,
            "output_path": str(outpath),
            "brand_id": brand_id,
        })
        return jsonify({
            "image_url": f"/api/image/{outpath.name}?date={datetime.now().strftime('%Y-%m-%d')}&fmt={format_name}",
            "path": str(outpath),
            "version": version,
            "model": inferred_model,
            "cost": cost,
            "estimated_remaining": f"${load_costs()['total']:.2f}",
        })
    return jsonify({"error": "Generation failed"}), 500


@app.route("/api/revise", methods=["POST"])
def api_revise():
    body = request.json or {}
    prompt = body.get("prompt", "").strip()
    input_path = body.get("input_image", "")
    session_id = body.get("session_id")
    annotations = body.get("annotations", [])
    format_name = body.get("format", "facebook-feed")
    model = body.get("model", "nano-banana-2")

    if not prompt or not input_path:
        return jsonify({"error": "Prompt and input image required"}), 400

    full_prompt = f"Edit this image: {prompt}. Keep the main subject. "
    if annotations:
        for a in annotations:
            full_prompt += f" At approximately x={a['x']:.0f}%, y={a['y']:.0f}%: {a['comment']}."

    fspec = FORMAT_SPECS.get(format_name, FORMAT_SPECS["facebook-feed"])
    outdir = OUTPUT_DIR / datetime.now().strftime("%Y-%m-%d") / format_name / "revisions"
    outdir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%H%M%S")
    filename = f"{ts}-revised.png"
    outpath = outdir / filename

    result = gen_image(full_prompt, outpath, model=model, resolution=fspec["resolution"], input_image=input_path)

    if result:
        cost = MODEL_COSTS.get(model, 0.07)
        track_cost(model)
        version = add_session_entry(session_id, {
            "type": "revise",
            "prompt": prompt,
            "model": model,
            "cost": cost,
            "annotations": annotations,
            "output_path": str(outpath),
        }) if session_id else 0
        return jsonify({
            "image_url": f"/api/image/{outpath.name}?date={datetime.now().strftime('%Y-%m-%d')}&fmt={format_name}&sub=revisions",
            "path": str(outpath),
            "version": version,
            "cost": cost,
        })
    return jsonify({"error": "Revision failed"}), 500


@app.route("/api/image/<path:filename>")
def serve_image(filename):
    date_q = request.args.get("date", datetime.now().strftime("%Y-%m-%d"))
    fmt = request.args.get("fmt", "")
    sub = request.args.get("sub", "")
    search = OUTPUT_DIR / date_q / fmt
    if sub:
        search = search / sub
    return send_from_directory(search, filename)


@app.route("/api/session", methods=["POST"])
def api_session_create():
    body = request.json or {}
    name = body.get("name", "").strip() or f"Session {datetime.now().strftime('%b %-d, %H:%M')}"
    session_id = str(uuid.uuid4())[:8]
    data = {
        "id": session_id,
        "name": name,
        "created": datetime.now(timezone.utc).isoformat(),
        "entries": [],
        "total_cost": 0.0,
    }
    save_session(session_id, data)
    return jsonify(data)


@app.route("/api/sessions", methods=["GET"])
def api_sessions_list():
    sessions = []
    for p in sorted(SESSIONS_DIR.glob("*.json"), reverse=True):
        data = load_json(p, {})
        if data:
            sessions.append({
                "id": data.get("id"),
                "name": data.get("name", "Untitled"),
                "created": data.get("created"),
                "entry_count": len(data.get("entries", [])),
                "total_cost": data.get("total_cost", 0),
            })
    costs = load_costs()
    return jsonify({"sessions": sessions, "global": costs})


@app.route("/api/session/<session_id>", methods=["GET"])
def api_session_get(session_id):
    data = load_session(session_id)
    return jsonify(data)


@app.route("/api/brands", methods=["GET", "POST"])
def api_brands():
    if request.method == "POST":
        body = request.json or {}
        name = body.get("name", "").strip()
        if not name:
            return jsonify({"error": "Name required"}), 400
        brands = load_brands()
        slug = re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")
        brands[slug] = {
            "id": slug,
            "name": name,
            "style_summary": body.get("style_summary", ""),
            "colors": body.get("colors", []),
            "fonts": body.get("fonts", []),
            "rules": body.get("rules", []),
        }
        save_brands(brands)
        return jsonify(brands[slug])
    brands = load_brands()
    return jsonify(list(brands.values()))


@app.route("/api/costs", methods=["GET"])
def api_costs():
    return jsonify(load_costs())


@app.route("/api/approve", methods=["POST"])
def api_approve():
    body = request.json or {}
    path = body.get("path", "")
    tags = body.get("tags", [])
    if not path:
        return jsonify({"error": "path required"}), 400
    approved_dir = DATA_DIR / "approved"
    approved_dir.mkdir(parents=True, exist_ok=True)
    dest = approved_dir / Path(path).name
    import shutil
    shutil.copy(path, dest)
    # Save approval metadata
    meta = {"path": str(dest), "tags": tags, "approved_at": datetime.now(timezone.utc).isoformat(), "original": path}
    meta_file = approved_dir / f"{Path(path).stem}.meta.json"
    save_json(meta_file, meta)
    return jsonify({"ok": True, "path": str(dest)})


@app.route("/api/open-folder", methods=["POST"])
def api_open_folder():
    folder = str(OUTPUT_DIR)
    if platform.system() == "Windows" or os.environ.get("WSL_DISTRO_NAME"):
        subprocess.run(["cmd.exe", "/c", "explorer", folder.replace("/mnt/c/", "C:\\").replace("/", "\\")], capture_output=True)
    else:
        subprocess.run(["open", folder], capture_output=True)
    return jsonify({})


# ── Main ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", "-p", type=int, default=5173)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    print("")
    print("  🎨 Creative Studio")
    print(f"  → http://localhost:{args.port}")
    print(f"  → Data: {DATA_DIR}")
    print(f"  → Outputs: {OUTPUT_DIR}")
    print("")
    app.run(host=args.host, port=args.port, debug=False)
