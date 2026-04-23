#!/usr/bin/env python3
"""
Creative Studio Web App v4.5
Flask backend with session management, cost tracking, generation, composite, export, QC.
Serves built-in frontend template.
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
from pathlib import Path
from datetime import datetime
from io import BytesIO
from typing import Optional, List, Dict

from flask import Flask, render_template_string, request, jsonify, send_from_directory

# ─── Config ────────────────────────────────────────────────────────────
API_KEY = os.environ.get("GEMINI_API_KEY")

# Session / cost / output dirs
DATA_DIR = Path.home() / ".creative-studio-data"
SESSIONS_DIR = DATA_DIR / "sessions"
COST_DB = DATA_DIR / "costs.json"

# Match CLI output directory logic exactly
if os.environ.get("CREATIVE_OUTPUT_DIR"):
    OUTPUT_DIR = Path(os.environ["CREATIVE_OUTPUT_DIR"])
elif Path("/mnt/c/Users").exists():
    _win_dl = Path("/mnt/c/Users/camst/Downloads/creative-studio-outputs")
    _win_dl.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR = _win_dl
else:
    OUTPUT_DIR = Path.home() / "creative-studio-outputs"

DATA_DIR.mkdir(parents=True, exist_ok=True)
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(Path(__file__).parent))

# ─── Helpers ───────────────────────────────────────────────────────────

def load_json(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else (default if default is not None else {})

def save_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))

# ─── Cost tracking ───────────────────────────────────────────────────
COSTS = {
    "gemini-3.1-flash-image-preview": 0.07,
    "gemini-3-pro-image-preview": 0.20,
    "imagen-4.0-fast-generate-001": 0.02,
    "imagen-4.0-generate-001": 0.04,
    "imagen-4.0-ultra-generate-001": 0.06,
}

def load_costs():
    return load_json(COST_DB, {"total": 0.0, "by_model": {}, "by_date": {}, "session_count": 0, "image_count": 0})

def save_costs(data: dict):
    save_json(COST_DB, data)

def track_cost(model: str, count: int = 1):
    costs = load_costs()
    c = COSTS.get(model, 0.04) * count
    costs["total"] += c
    costs["by_model"][model] = costs["by_model"].get(model, 0.0) + c
    today = datetime.now().strftime("%Y-%m-%d")
    costs["by_date"][today] = costs["by_date"].get(today, 0.0) + c
    costs["image_count"] += count
    save_costs(costs)
    return c

def session_cost(session_id: str) -> float:
    return sum(e.get("cost", 0) for e in load_session(session_id).get("entries", []))

# ─── Session management ────────────────────────────────────────────────

def new_session_id():
    return "sess_" + uuid.uuid4().hex[:8]

def session_path(session_id: str) -> Path:
    return SESSIONS_DIR / f"{session_id}.json"

def load_session(session_id: str) -> dict:
    return load_json(session_path(session_id), {"id": session_id, "created_at": now_str(), "entries": []})

def save_session(session_id: str, data: dict):
    save_json(session_path(session_id), data)

def add_entry(session_id: str, entry: dict):
    data = load_session(session_id)
    data["entries"].append({"time": now_str(), **entry})
    save_session(session_id, data)

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def image_url(path: str) -> str:
    if not path:
        return ""
    p = Path(path)
    try:
        rel = p.relative_to(OUTPUT_DIR)
        return f"/image/{rel}"
    except (ValueError, NotImplementedError):
        if p.exists():
            return f"/image/{p.parent.name}/{p.name}"
        return ""

# ─── Pin Annotations ────────────────────────────────────────────────────
PINS_DB = DATA_DIR / "pins.json"

def load_pins(image_path: str) -> List[Dict]:
    data = load_json(PINS_DB, {})
    return data.get(image_path, [])

def save_pins(image_path: str, pins: List[Dict]):
    data = load_json(PINS_DB, {})
    data[image_path] = pins
    save_json(PINS_DB, data)

def pin_id() -> str:
    return uuid.uuid4().hex[:8]

def pin_to_region(x: float, y: float) -> str:
    """Convert normalized coords (0..1) to human-readable region."""
    h = "top" if y < 0.33 else "middle" if y < 0.66 else "bottom"
    w = "left" if x < 0.33 else "center" if x < 0.66 else "right"
    if h == "middle" and w == "center":
        return "center"
    return f"{h}-{w}" if h != "middle" else w

def build_pin_prompt(pins: List[Dict]) -> str:
    """Build a spatially-aware refinement prompt from pins."""
    if not pins:
        return ""
    lines = []
    for i, p in enumerate(pins, 1):
        region = pin_to_region(p.get("x", 0.5), p.get("y", 0.5))
        text = p.get("text", "").strip()
        if text:
            lines.append(f"[{i}] {region}: {text}")
    if not lines:
        return ""
    return (
        "Make these targeted changes to specific areas of the image: "
        + "; ".join(lines) +
        ". Apply each change only to its specified region. Preserve all other areas exactly as they are."
    )

# ─── Image generation wrappers ────────────────────────────────────────

def run_cli_generate(prompt: str, mode: str, tier: str, aspect: str, smart: bool,
                     input_image: Optional[str] = None, variations: int = 4) -> List[Dict]:
    """Call creative_studio.py CLI and return list of generated paths."""
    out_dir = OUTPUT_DIR / datetime.now().strftime("%Y-%m-%d") / mode
    out_dir.mkdir(parents=True, exist_ok=True)

    args = [sys.executable, "-m", "creative_studio"]
    if mode == "variations":
        args = ["bash", str(Path(__file__).parent.parent / "launch.sh"), "variations",
                "--prompt", prompt, "--tier", tier, "--aspect-ratio", aspect,
                "--format", mode,
                "-v", str(variations)]
    else:
        args = ["bash", str(Path(__file__).parent.parent / "launch.sh"), "direct",
                "--prompt", prompt, "--tier", tier, "--aspect-ratio", aspect,
                "--format", mode]
        if smart:
            args.append("--smart")
    if input_image:
        args += ["--input-image", input_image]

    env = os.environ.copy()
    env["GEMINI_API_KEY"] = API_KEY

    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=300, env=env)
        # Find generated files in output dir
        files = sorted(out_dir.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
        # Return newest files
        images = []
        for f in files[:variations]:
            model_used = "gemini-3.1-flash-image-preview" if tier in ("fast", "balanced") else "gemini-3-pro-image-preview"
            cost = track_cost(model_used)
            images.append({"path": str(f), "url": image_url(str(f)),
                           "name": f.name, "cost": cost, "model": model_used})
        return images
    except Exception as e:
        return [{"error": str(e)}]


def run_cli_composite(prompt: str, product_path: str, aspect: str, tier: str = "quality") -> List[Dict]:
    out_dir = OUTPUT_DIR / datetime.now().strftime("%Y-%m-%d") / "composite"
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"composite-{int(time.time())}.png"
    out_path = out_dir / fname

    args = ["bash", str(Path(__file__).parent.parent / "launch.sh"), "composite",
            "--prompt", prompt, "--product", product_path,
            "--aspect-ratio", aspect, "--tier", tier, "--filename", fname]
    env = os.environ.copy()
    env["GEMINI_API_KEY"] = API_KEY

    try:
        subprocess.run(args, capture_output=True, text=True, timeout=300, env=env, check=False)
        if out_path.exists():
            model_used = "gemini-3-pro-image-preview"
            cost = track_cost(model_used)
            return [{"path": str(out_path), "url": image_url(str(out_path)),
                     "name": fname, "cost": cost, "model": model_used}]
    except Exception as e:
        pass
    return []


def run_cli_export(source_path: str, presets: str) -> List[Dict]:
    args = ["bash", str(Path(__file__).parent.parent / "launch.sh"), "export",
            "--input", source_path, "--presets", presets]
    env = os.environ.copy()
    env["GEMINI_API_KEY"] = API_KEY
    try:
        subprocess.run(args, capture_output=True, text=True, timeout=120, env=env, check=False)
        out_dir = OUTPUT_DIR / datetime.now().strftime("%Y-%m-%d") / "exports"
        files = list(out_dir.glob("*.png"))
        images = []
        for f in files:
            images.append({"path": str(f), "url": image_url(str(f)),
                           "name": f.name, "cost": 0.0, "model": "PIL"})
        return images
    except Exception:
        return []


def run_cli_qc(image_path: str) -> dict:
    args = ["bash", str(Path(__file__).parent.parent / "launch.sh"), "qc",
            "--input", image_path]
    env = os.environ.copy()
    env["GEMINI_API_KEY"] = API_KEY
    # Parse stdout for QC results
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=120, env=env, check=False)
        out = result.stdout + result.stderr
        # Extract score
        score = 5
        m = re.search(r"QC SCORE:\s*(\d)/10", out)
        if m:
            score = int(m.group(1))
        floating = "FAIL" in out and "Floating" in out
        garbled = "FAIL" in out and "Garbled" in out
        shadows = "FAIL" in out and "Shadows" in out
        fake = "FAIL" in out and "Fake" in out
        labels = "PASS" in out and "Labels" in out
        # Extract issues
        issues = []
        for line in out.split("\n"):
            if "⚠" in line:
                issues.append(line.replace("⚠", "").strip())
        return {
            "quality_score": score,
            "floating_products": floating,
            "garbled_text": garbled,
            "detached_shadows": shadows,
            "fake_products": fake,
            "readable_labels": labels,
            "issues": issues[:5],
        }
    except Exception as e:
        return {"quality_score": 0, "error": str(e), "issues": []}


def run_cli_refine(image_path: str, changes: str, tier: str) -> List[Dict]:
    # Since refine needs a session folder from variations, we'll do a "revise" via direct mode
    # with the original image as reference + changes in prompt
    out_dir = OUTPUT_DIR / datetime.now().strftime("%Y-%m-%d") / "refine"
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"refine-{int(time.time())}.png"

    # Build revised prompt
    args = ["bash", str(Path(__file__).parent.parent / "launch.sh"), "direct",
            "--prompt", f"Based on this reference image, make these changes: {changes}",
            "--input-image", image_path,
            "--tier", tier, "--filename", fname]
    env = os.environ.copy()
    env["GEMINI_API_KEY"] = API_KEY
    try:
        subprocess.run(args, capture_output=True, text=True, timeout=300, env=env, check=False)
        out_path = out_dir / fname
        if not out_path.exists():
            # Fallback: search for any newly created png in output dir
            today_dir = OUTPUT_DIR / datetime.now().strftime("%Y-%m-%d")
            files = sorted(today_dir.rglob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
            if files:
                out_path = files[0]
        if out_path.exists():
            model_used = "gemini-3.1-flash-image-preview" if tier in ("fast", "balanced") else "gemini-3-pro-image-preview"
            cost = track_cost(model_used)
            return [{"path": str(out_path), "url": image_url(str(out_path)),
                     "name": out_path.name, "cost": cost, "model": model_used}]
    except Exception:
        pass
    return []


# ─── Flask App ─────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32MB uploads

# ── Frontend HTML ─────────────────────────────────────────────────────

HTML_TEMPLATE = r'''
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Creative Studio v5 — AI Design Canvas</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #0B0E14;
  --bg-elevated: #121620;
  --surface: rgba(18, 22, 32, 0.75);
  --border: rgba(255,255,255,0.06);
  --border-hover: rgba(255,255,255,0.12);
  --text: #E8ECF1;
  --text-muted: #64748B;
  --primary: #FF7A59;
  --primary-dim: rgba(255,122,89,0.15);
  --accent: #FFD166;
  --success: #06D6A0;
  --danger: #EF476F;
  --font: 'Inter', system-ui, -apple-system, sans-serif;
  --font-mono: 'JetBrains Mono', monospace;
  --radius: 14px;
  --radius-sm: 8px;
  --shadow: 0 8px 32px rgba(0,0,0,0.4);
}

* { box-sizing:border-box; margin:0; padding:0 }
html, body { height:100%; }
body {
  font-family: var(--font);
  background: var(--bg);
  color: var(--text);
  overflow: hidden;
  line-height: 1.5;
}

/* Animated background mesh */
body::before {
  content: '';
  position: fixed;
  inset: 0;
  background:
    radial-gradient(ellipse 80% 50% at 20% 40%, rgba(255,122,89,0.08) 0%, transparent 50%),
    radial-gradient(ellipse 60% 40% at 80% 60%, rgba(255,209,102,0.05) 0%, transparent 50%);
  pointer-events: none;
  z-index: 0;
  animation: meshMove 20s ease-in-out infinite;
}
@keyframes meshMove {
  0%, 100% { opacity: 0.6; transform: scale(1); }
  50% { opacity: 1; transform: scale(1.1); }
}

.app { display: grid; grid-template-rows: 52px 1fr; height: 100vh; position: relative; z-index: 1; }

/* ── Header ────────────────────────────────────────── */
header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 24px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-elevated);
  backdrop-filter: blur(20px);
  -webkit-backdrop-filter: blur(20px);
}
header .brand {
  display: flex; align-items: center; gap: 12px;
  font-weight: 700; font-size: 18px; letter-spacing: -0.3px;
}
header .brand .dot {
  width: 10px; height: 10px; border-radius: 50%;
  background: var(--primary); box-shadow: 0 0 12px var(--primary);
  animation: pulse 2s ease-in-out infinite;
}
@keyframes pulse { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.6; transform: scale(1.2); } }
header .brand span { color: #94a3b8; font-weight: 500; font-size: 14px; }

.header-actions { display: flex; align-items: center; gap: 10px; }
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  padding: 8px 16px; border-radius: var(--radius-sm);
  border: 1px solid rgba(255,255,255,0.1);
  background: rgba(255,255,255,0.04); color: #e2e8f0;
  font-family: var(--font); font-size: 14px; font-weight: 500;
  cursor: pointer; transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
}
.btn:hover { border-color: rgba(255,255,255,0.2); color: #fff; background: rgba(255,255,255,0.08); }
.btn.primary { background: var(--primary); border-color: var(--primary); color: #fff; font-weight: 600; }
.btn.primary:hover { background: #ff6340; border-color: #ff6340; transform: translateY(-1px); box-shadow: 0 4px 16px rgba(255,122,89,0.3); }
.btn.accent { background: var(--accent); border-color: var(--accent); color: #0B0E14; font-weight: 600; }
.btn.accent:hover { background: #ffc94d; transform: translateY(-1px); }
.btn.sm { padding: 6px 12px; font-size: 13px; }
.btn.ghost { border: none; background: transparent; color: #94a3b8; }
.btn.ghost:hover { background: rgba(255,255,255,0.06); color: #e2e8f0; }

/* ── Main Grid ─────────────────────────────────────── */
.main { display: grid; grid-template-columns: 340px 1fr 300px; overflow: hidden; }

/* ── Panels ─────────────────────────────────────────── */
.panel {
  background: rgba(18, 22, 32, 0.7);
  border-right: 1px solid rgba(255,255,255,0.06);
  backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
  display: flex; flex-direction: column;
  overflow: hidden;
}
.panel:last-child { border-right: none; border-left: 1px solid rgba(255,255,255,0.06); }

.panel-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 16px 20px 12px;
  font-size: 13px; font-weight: 700;
  letter-spacing: 0.02em; color: #e2e8f0;
  border-bottom: 1px solid rgba(255,255,255,0.06);
  background: rgba(0,0,0,0.15);
}
.panel-header .icon { font-size: 15px; margin-right: 6px; opacity: 0.7; }
.panel-body { flex: 1; overflow-y: auto; padding: 16px 20px 20px; }
.panel-body::-webkit-scrollbar { width: 5px; }
.panel-body::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 4px; }
.panel-body::-webkit-scrollbar-track { background: transparent; }

/* ── Nav Tabs ─────────────────────────────────────── */
.nav-tabs {
  display: flex; gap: 3px; padding: 4px;
  background: rgba(0,0,0,0.25); border-radius: var(--radius-sm);
  margin-bottom: 20px;
  border: 1px solid rgba(255,255,255,0.05);
}
.nav-tab {
  flex: 1; text-align: center; padding: 8px 0;
  border-radius: calc(var(--radius-sm) - 2px);
  font-size: 13px; font-weight: 500; color: #64748B;
  cursor: pointer; transition: all 0.2s; border: none; background: transparent;
}
.nav-tab:hover { color: #e2e8f0; }
.nav-tab.active {
  background: rgba(255,255,255,0.08); color: #e2e8f0;
  box-shadow: 0 2px 8px rgba(0,0,0,0.2);
  font-weight: 600;
}
.nav-tab .shortcut {
  font-family: var(--font-mono); font-size: 10px; opacity: 0.5;
  background: rgba(255,255,255,0.08); padding: 2px 5px; border-radius: 4px;
  margin-left: 5px;
}

/* ── Form ──────────────────────────────────────────── */
.form-group { margin-bottom: 18px; }
.form-group label {
  display: block; font-size: 12px; font-weight: 600;
  color: #94a3b8; margin-bottom: 6px;
}
.form-group .help {
  font-size: 11px; color: #475569; margin-top: 4px; line-height: 1.4;
}
input, textarea, select {
  width: 100%; padding: 10px 12px; border-radius: var(--radius-sm);
  border: 1px solid rgba(255,255,255,0.1); background: rgba(0,0,0,0.3);
  color: var(--text); font-family: var(--font); font-size: 14px;
  transition: border-color 0.2s, box-shadow 0.2s;
  line-height: 1.5;
}
input:focus, textarea:focus, select:focus {
  outline: none; border-color: var(--primary); box-shadow: 0 0 0 3px rgba(255,122,89,0.15);
}
input::placeholder, textarea::placeholder { color: #475569; }
select { cursor: pointer; appearance: none; background-image: url("data:image/svg+xml,%3Csvg width='12' height='12' fill='%2394a3b8' viewBox='0 0 12 12'%3E%3Cpath d='M3 4.5l3 3 3-3'/%3E%3C/svg%3E"); background-repeat: no-repeat; background-position: right 10px center; padding-right: 30px; }

/* Section divider */
.section-divider {
  height: 1px; background: var(--border); margin: 16px 0; position: relative;
}
.section-divider span {
  position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%);
  background: var(--surface); padding: 0 10px;
  font-size: 10px; color: #475569; text-transform: uppercase; letter-spacing: 0.1em;
}
/* Checkbox */
.checkbox {
  display: flex; align-items: center; gap: 8px; cursor: pointer; font-size: 13px; color: var(--text-muted);
}
.checkbox input { width: auto; accent-color: var(--primary); }

/* ── Dropzone ──────────────────────────────────────── */
.dropzone {
  border: 2px dashed var(--border); border-radius: var(--radius-sm);
  padding: 24px 12px; text-align: center; cursor: pointer;
  transition: all 0.3s cubic-bezier(0.16, 1, 0.3, 1); position: relative; overflow: hidden;
}
.dropzone::before {
  content: ''; position: absolute; inset: 0;
  background: radial-gradient(circle at center, var(--primary-dim) 0%, transparent 70%);
  opacity: 0; transition: opacity 0.3s;
}
.dropzone:hover, .dropzone.dragover {
  border-color: var(--primary); transform: scale(1.01);
}
.dropzone:hover::before, .dropzone.dragover::before { opacity: 1; }
.dropzone .dz-inner { position: relative; z-index: 1; }
.dropzone img { max-height: 120px; border-radius: 6px; display: block; margin: 8px auto 0; box-shadow: var(--shadow); }
.dropzone .dz-icon { font-size: 28px; margin-bottom: 8px; opacity: 0.5; }

/* ── Quick Tags ────────────────────────────────────── */
.tag-list { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
.tag {
  padding: 4px 10px; border-radius: 20px; font-size: 11px; font-weight: 500;
  background: rgba(255,255,255,0.04); border: 1px solid var(--border);
  color: var(--text-muted); cursor: pointer; transition: all 0.15s;
}
.tag:hover { border-color: var(--primary); color: var(--primary); background: var(--primary-dim); }
.tag.active { background: var(--primary); border-color: var(--primary); color: #fff; }

/* ── Cost Bar ──────────────────────────────────────── */
.cost-bar {
  display: flex; align-items: center; gap: 8px; padding: 10px 14px;
  background: rgba(255,209,102,0.08); border: 1px solid rgba(255,209,102,0.15);
  border-radius: var(--radius-sm); font-size: 12px; color: var(--accent); margin-top: 10px;
}
.cost-bar .amount { font-family: var(--font-mono); font-weight: 700; font-size: 16px; color: var(--accent); }
.cost-bar .sparkline {
  flex: 1; height: 20px; display: flex; align-items: flex-end; gap: 2px; opacity: 0.5;
}
.cost-bar .sparkline .bar { flex: 1; background: var(--accent); border-radius: 1px; min-height: 2px; }

/* ── Model Cards ───────────────────────────────────── */
.model-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 6px; }
.model-card {
  padding: 10px; border-radius: var(--radius-sm); border: 1px solid var(--border);
  background: var(--bg-elevated); cursor: pointer; transition: all 0.2s;
}
.model-card:hover { border-color: var(--border-hover); }
.model-card.selected { border-color: var(--primary); background: var(--primary-dim); }
.model-card .name { font-size: 12px; font-weight: 600; }
.model-card .price { font-size: 11px; color: var(--text-muted); margin-top: 2px; }

/* ── Center Canvas ────────────────────────────────── */
.canvas-area {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  min-height: 300px; position: relative; overflow: hidden;
}
.canvas-area.empty {
  background: var(--bg-elevated); border-radius: var(--radius); border: 1px dashed var(--border);
}
.canvas-area img {
  max-width: 90%; max-height: 400px; border-radius: 8px;
  box-shadow: var(--shadow);
  animation: appear 0.6s cubic-bezier(0.16, 1, 0.3, 1);
}
@keyframes appear {
  from { opacity: 0; transform: scale(0.95); }
  to { opacity: 1; transform: scale(1); }
}

/* Shimmer loading */
.shimmer {
  position: absolute; inset: 0; overflow: hidden;
}
.shimmer::after {
  content: ''; position: absolute; top: 0; left: -100%; width: 100%; height: 100%;
  background: linear-gradient(90deg, transparent, rgba(255,255,255,0.03), transparent);
  animation: shimmerSlide 1.5s infinite;
}
@keyframes shimmerSlide { to { left: 100%; } }

/* ── Preview Grid ──────────────────────────────────── */
.preview-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 10px;
}
.preview-item {
  position: relative; border-radius: 10px; overflow: hidden;
  border: 1px solid var(--border); cursor: pointer;
  transition: all 0.3s cubic-bezier(0.16, 1, 0.3, 1);
  background: var(--bg-elevated);
}
.preview-item:hover { transform: translateY(-3px); border-color: var(--border-hover); box-shadow: 0 8px 24px rgba(0,0,0,0.3); }
.preview-item.selected { outline: 2px solid var(--primary); outline-offset: 2px; }
.preview-item img { width: 100%; height: auto; display: block; }
.preview-item .meta {
  position: absolute; bottom: 0; left: 0; right: 0;
  padding: 6px 8px; background: rgba(0,0,0,0.7); backdrop-filter: blur(4px);
  font-size: 10px; color: var(--text-muted); display: flex; justify-content: space-between;
  opacity: 0; transition: opacity 0.2s;
}
.preview-item:hover .meta { opacity: 1; }
.preview-item .cost-tag {
  position: absolute; top: 6px; right: 6px;
  background: rgba(0,0,0,0.6); backdrop-filter: blur(8px);
  padding: 2px 6px; border-radius: 10px; font-size: 10px; color: var(--accent); font-weight: 600;
}

/* ── QC Dashboard ─────────────────────────────────── */
.qc-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 10px; }
.qc-item {
  padding: 12px; border-radius: var(--radius-sm); border: 1px solid var(--border);
  background: var(--bg-elevated); text-align: center; transition: all 0.2s;
}
.qc-item.pass { border-color: rgba(6,214,160,0.3); background: rgba(6,214,160,0.06); }
.qc-item.fail { border-color: rgba(239,71,111,0.3); background: rgba(239,71,111,0.06); }
.qc-item .status-icon { font-size: 20px; margin-bottom: 4px; }
.qc-item .label { font-size: 10px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; }
.qc-score-ring {
  width: 80px; height: 80px; border-radius: 50%;
  border: 4px solid var(--border); position: relative; margin: 0 auto 12px;
  display: flex; align-items: center; justify-content: center;
}
.qc-score-ring .score { font-family: var(--font-mono); font-size: 24px; font-weight: 700; }

/* ── Export Presets ────────────────────────────────── */
.preset-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; margin-top: 8px; }
.preset-card {
  padding: 12px; border-radius: var(--radius-sm); border: 1px solid var(--border);
  background: var(--bg-elevated); cursor: pointer; position: relative;
  transition: all 0.2s; text-align: center;
}
.preset-card:hover { border-color: var(--border-hover); }
.preset-card.checked { border-color: var(--primary); background: var(--primary-dim); }
.preset-card .aspect-box {
  width: 36px; height: 28px; border: 2px solid var(--text-muted); border-radius: 3px;
  margin: 0 auto 6px; opacity: 0.5;
}
.preset-card.checked .aspect-box { border-color: var(--primary); opacity: 1; }
.preset-card .name { font-size: 11px; font-weight: 600; }
.preset-card .dims { font-size: 10px; color: var(--text-muted); margin-top: 2px; }

/* ── Export preset aspect boxes ────────────────────── */
.aspect-1-1 { width: 28px !important; height: 28px !important; }
.aspect-4-5 { width: 22px !important; height: 28px !important; }
.aspect-9-16 { width: 16px !important; height: 28px !important; }
.aspect-16-9 { width: 36px !important; height: 20px !important; }
.aspect-2-3 { width: 20px !important; height: 30px !important; }
.aspect-3-2 { width: 30px !important; height: 20px !important; }

/* ── Status Toast ──────────────────────────────────── */
.status-toast {
  position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%) translateY(100px);
  padding: 10px 20px; border-radius: 30px; font-size: 13px; font-weight: 500;
  z-index: 1000; transition: all 0.4s cubic-bezier(0.16, 1, 0.3, 1);
  backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px);
  box-shadow: 0 8px 32px rgba(0,0,0,0.4);
}
.status-toast.show { transform: translateX(-50%) translateY(0); }
.status-toast.success { background: rgba(6,214,160,0.15); border: 1px solid rgba(6,214,160,0.3); color: var(--success); }
.status-toast.error { background: rgba(239,71,111,0.15); border: 1px solid rgba(239,71,111,0.3); color: var(--danger); }
.status-toast.info { background: var(--surface); border: 1px solid var(--border); color: var(--text-muted); }

/* ── Session Row ────────────────────────────────────── */
.session-row {
  display: flex; gap: 10px; align-items: center; padding: 8px 0;
  border-bottom: 1px solid var(--border); cursor: pointer; transition: all 0.15s;
}
.session-row:hover { color: var(--primary); }
.session-row:last-child { border-bottom: none; }
.session-row img { width: 36px; height: 36px; object-fit: cover; border-radius: 6px; }
.session-row .meta { flex: 1; }
.session-row .meta .id { font-size: 12px; font-weight: 600; }
.session-row .meta .sub { font-size: 11px; color: var(--text-muted); }

/* ── Empty State ───────────────────────────────────── */
.empty-state { text-align: center; padding: 40px 20px; color: var(--text-muted); }
.empty-state .icon { font-size: 48px; margin-bottom: 12px; opacity: 0.3; }
.empty-state h3 { font-size: 16px; font-weight: 600; margin-bottom: 6px; color: var(--text); }
.empty-state p { font-size: 13px; max-width: 280px; margin: 0 auto; line-height: 1.6; }

/* ── Bottom Bar ───────────────────────────────────── */
.bottom-bar {
  position: fixed; bottom: 0; left: 0; right: 0;
  display: flex; align-items: center; justify-content: space-between;
  padding: 8px 20px; background: var(--bg-elevated);
  border-top: 1px solid var(--border);
  backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
  z-index: 100; font-size: 12px; color: var(--text-muted);
}
.bottom-bar .keys { display: flex; gap: 12px; }
.bottom-bar kbd {
  font-family: var(--font-mono); font-size: 10px; padding: 2px 6px;
  background: rgba(255,255,255,0.06); border-radius: 4px; border: 1px solid var(--border);
}

/* ── Responsive ──────────────────────────────────── */
@media (max-width: 1100px) {
  .main { grid-template-columns: 1fr; }
  .panel { display: none; }
  .panel.active { display: flex; position: fixed; inset: 52px 0 0 0; z-index: 50; }
  .canvas-area { min-height: 50vh; }
}

/* ── Loading spinner ──────────────────────────────── */
.spinner {
  width: 40px; height: 40px; border: 3px solid var(--border);
  border-top-color: var(--primary); border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* ── Confetti (simple CSS particles) ──────────────── */
.confetti { position: absolute; width: 6px; height: 6px; border-radius: 2px; pointer-events: none; }
/* ── Pin Annotations ───────────────────────────────── */
.pin-layer { position: absolute; inset: 0; pointer-events: none; z-index: 10; }
.pin-layer.active { pointer-events: all; cursor: crosshair; }
.pin-marker {
  position: absolute; width: 22px; height: 22px; transform: translate(-50%, -100%);
  cursor: pointer; pointer-events: all; z-index: 20; transition: transform 0.15s;
}
.pin-marker:hover { transform: translate(-50%, -100%) scale(1.2); }
.pin-marker .dot {
  width: 14px; height: 14px; border-radius: 50%; background: var(--primary);
  border: 2px solid #fff; box-shadow: 0 2px 8px rgba(0,0,0,0.4);
  position: absolute; bottom: 0; left: 50%; transform: translateX(-50%);
}
.pin-marker .tip {
  position: absolute; bottom: 20px; left: 50%; transform: translateX(-50%);
  background: var(--bg-elevated); border: 1px solid var(--border);
  padding: 6px 10px; border-radius: 8px; font-size: 11px; color: var(--text);
  white-space: nowrap; opacity: 0; transition: opacity 0.2s; pointer-events: none;
  box-shadow: 0 4px 16px rgba(0,0,0,0.3);
}
.pin-marker:hover .tip { opacity: 1; }
.pin-btn {
  position: absolute; top: 8px; right: 8px; z-index: 30;
  background: rgba(0,0,0,0.5); backdrop-filter: blur(8px);
  border: 1px solid var(--border); border-radius: 6px;
  padding: 4px 10px; font-size: 11px; color: var(--text-muted);
  cursor: pointer; transition: all 0.2s;
}
.pin-btn:hover { color: var(--primary); border-color: var(--primary); }
.pin-btn.active { color: var(--primary); background: var(--primary-dim); border-color: var(--primary); }
.canvas-wrap { position: relative; display: inline-block; }
.pin-confirm {
  position: absolute; background: var(--bg-elevated); border: 1px solid var(--border);
  border-radius: 8px; padding: 8px; box-shadow: 0 8px 32px rgba(0,0,0,0.4);
  z-index: 40; display: flex; gap: 6px; min-width: 220px;
}
.pin-confirm input {
  flex: 1; padding: 6px 10px; font-size: 12px; border-radius: 5px;
  border: 1px solid var(--border); background: var(--bg); color: var(--text);
}
.pin-confirm input:focus { outline: none; border-color: var(--primary); }
.pin-confirm .btn { padding: 5px 10px; font-size: 11px; }

/* ── Pin List in Inspector ─────────────────────────── */
.pin-list { display: flex; flex-direction: column; gap: 6px; margin-top: 8px; }
.pin-row {
  display: flex; align-items: flex-start; gap: 8px; padding: 8px 10px;
  background: var(--bg-elevated); border: 1px solid var(--border); border-radius: 6px;
  font-size: 12px; color: var(--text); transition: all 0.15s;
}
.pin-row:hover { border-color: var(--border-hover); }
.pin-row .region { font-weight: 600; color: var(--primary); flex: 0 0 auto; }
.pin-row .text { flex: 1; }
.pin-row .del { cursor: pointer; color: var(--text-muted); font-size: 14px; opacity: 0; transition: opacity 0.15s; }
.pin-row:hover .del { opacity: 1; }
.pin-row .del:hover { color: var(--danger); }
</style>
</head>
<body>
<div class="app">

<!-- ═══════ HEADER ═══════ -->
<header>
  <div class="brand">
    <div class="dot"></div>
    Creative <span>Studio</span>
  </div>
  <div class="header-actions">
    <button class="btn ghost" id="btn-theme" title="Theme">◐</button>
    <button class="btn primary" id="btn-generate-top">⚡ Generate</button>
  </div>
</header>

<!-- ═══════ MAIN ═══════ -->
<div class="main" id="main-grid">

<!-- ═══════ LEFT PANEL ═══════ -->
<div class="panel" id="panel-left">
  <div class="panel-header"><span class="icon">&#9881;</span> Generation Settings</div>
  <div class="panel-body">

    <!-- Mode Tabs -->
    <div class="nav-tabs" id="mode-tabs">
      <button class="nav-tab active" data-mode="generate">Generate <span class="shortcut">G</span></button>
      <button class="nav-tab" data-mode="composite">Composite</button>
      <button class="nav-tab" data-mode="export">Export</button>
      <button class="nav-tab" data-mode="qc">QC</button>
    </div>

    <!-- MODE: GENERATE -->
    <div id="mode-generate">
      <div class="form-group">
        <label>Describe what you want</label>
        <textarea id="prompt-input" placeholder="A single red apple on a white table, product photography, overhead softbox lighting, shallow depth of field, Shot on Hasselblad H6D, 100mm f/2.8..."></textarea>
        <div class="help">Be specific about subject, lighting, camera angle, and style. Click tags below to add common photo terms.</div>
      </div>

      <div class="form-group">
        <label>Quick Tags <span style="font-weight:400;color:#475569;">— click to add</span></label>
        <div class="tag-list" id="quick-tags">
          <span class="tag" data-text="overhead softbox lighting">💡 Lighting</span>
          <span class="tag" data-text="shallow depth of field">🎬 DoF</span>
          <span class="tag" data-text="Shot on Hasselblad H6D">📷 Camera</span>
          <span class="tag" data-text="clean white background">⬜ White BG</span>
          <span class="tag" data-text="eye-level angle">👁️ Eye Level</span>
          <span class="tag" data-text="professional product photography">🏢 Studio</span>
        </div>
      </div>

      <div class="section-divider"><span>Model Settings</span></div>

      <div class="form-group">
        <label>Mode</label>
        <select id="gen-mode">
          <option value="direct">Direct (one-shot)</option>
          <option value="variations">Variations (4-pack)</option>
        </select>
      </div>
      <div class="form-group hidden" id="variations-count-group">
        <label>Count</label>
        <input type="number" id="variations-count" value="4" min="1" max="8">
      </div>

      <div class="form-group">
        <label>Tier</label>
        <div class="model-grid" id="tier-grid">
          <div class="model-card" data-tier="fast">
            <div class="name">Flash</div>
            <div class="price">~$0.07 · 1K</div>
          </div>
          <div class="model-card selected" data-tier="balanced">
            <div class="name">Balanced</div>
            <div class="price">~$0.07 · 2K</div>
          </div>
          <div class="model-card" data-tier="quality">
            <div class="name">Quality</div>
            <div class="price">~$0.20 · 2K</div>
          </div>
          <div class="model-card" data-tier="ultra">
            <div class="name">Ultra</div>
            <div class="price">~$0.40 · 4K</div>
          </div>
        </div>
      </div>

      <div class="form-group">
        <label>Aspect Ratio</label>
        <select id="aspect-select">
          <option value="16:9">16:9 Widescreen</option>
          <option value="1:1">1:1 Square</option>
          <option value="4:5">4:5 Instagram</option>
          <option value="9:16">9:16 Stories</option>
          <option value="3:2">3:2 Print</option>
        </select>
      </div>

      <div class="form-group">
        <label>Reference Image</label>
        <div class="dropzone" id="dz-ref">
          <div class="dz-inner">
            <div class="dz-icon">🖼️</div>
            <div>Drop reference image</div>
            <small style="color:var(--text-muted)">Optional — helps preserve subject</small>
          </div>
          <input type="file" id="ref-file" accept="image/*" style="display:none">
        </div>
      </div>

      <div class="form-group">
        <label class="checkbox"><input type="checkbox" id="smart-check" checked> Smart prompt enhancement</label>
      </div>

      <button class="btn primary" id="btn-generate" style="width:100%;padding:12px">⚡ Generate</button>
    </div>

    <!-- MODE: COMPOSITE -->
    <div id="mode-composite" class="hidden">
      <div class="form-group">
        <label>Product Photo</label>
        <div class="dropzone" id="dz-product">
          <div class="dz-inner">
            <div class="dz-icon">📦</div>
            <div>Drop your product photo</div>
            <small style="color:var(--text-muted)">AI will composite onto background</small>
          </div>
          <input type="file" id="product-file" accept="image/*" style="display:none">
        </div>
      </div>
      <div class="form-group">
        <label>Environment Prompt</label>
        <textarea id="composite-prompt" placeholder="Empty clean light wooden retail shelves in a premium supplement store. Warm overhead track lighting. No products anywhere."></textarea>
      </div>
      <div class="form-group">
        <label>Aspect Ratio</label>
        <select id="composite-aspect">
          <option value="16:9">16:9</option>
          <option value="1:1">1:1</option>
          <option value="4:5">4:5</option>
          <option value="9:16">9:16</option>
        </select>
      </div>
      <button class="btn primary" id="btn-composite" style="width:100%;padding:12px">🔧 Generate Composite</button>
    </div>

    <!-- MODE: EXPORT -->
    <div id="mode-export" class="hidden">
      <div class="form-group">
        <label>Source Image</label>
        <div class="dropzone" id="dz-export">
          <div class="dz-inner">
            <div class="dz-icon">📤</div>
            <div>Drop image to export</div>
          </div>
          <input type="file" id="export-file" accept="image/*" style="display:none">
        </div>
      </div>
      <div class="form-group">
        <label>Platform Presets</label>
        <div class="preset-grid" id="preset-grid">
          <div class="preset-card checked" data-preset="amazon">
            <div class="aspect-box aspect-1-1"></div>
            <div class="name">Amazon</div>
            <div class="dims">2000×2000</div>
          </div>
          <div class="preset-card checked" data-preset="shopify">
            <div class="aspect-box aspect-1-1"></div>
            <div class="name">Shopify</div>
            <div class="dims">2048×2048</div>
          </div>
          <div class="preset-card checked" data-preset="meta-feed">
            <div class="aspect-box aspect-4-5"></div>
            <div class="name">Meta Feed</div>
            <div class="dims">1080×1350</div>
          </div>
          <div class="preset-card" data-preset="meta-stories">
            <div class="aspect-box aspect-9-16"></div>
            <div class="name">Stories</div>
            <div class="dims">1080×1920</div>
          </div>
          <div class="preset-card checked" data-preset="web-hero">
            <div class="aspect-box aspect-16-9"></div>
            <div class="name">Web Hero</div>
            <div class="dims">1920×1080</div>
          </div>
          <div class="preset-card" data-preset="pinterest">
            <div class="aspect-box aspect-2-3"></div>
            <div class="name">Pinterest</div>
            <div class="dims">1000×1500</div>
          </div>
        </div>
      </div>
      <button class="btn primary" id="btn-export" style="width:100%;padding:12px">📦 Export</button>
    </div>

    <!-- MODE: QC -->
    <div id="mode-qc" class="hidden">
      <div class="form-group">
        <label>Image to Inspect</label>
        <div class="dropzone" id="dz-qc">
          <div class="dz-inner">
            <div class="dz-icon">🔍</div>
            <div>Drop image to check</div>
          </div>
          <input type="file" id="qc-file" accept="image/*" style="display:none">
        </div>
      </div>
      <button class="btn primary" id="btn-qc" style="width:100%;padding:12px">🔍 Run Quality Check</button>
      <div id="qc-results"></div>
    </div>

    <!-- Cost Bar -->
    <div class="cost-bar" id="cost-bar">
      <div class="amount" id="cost-amount">$0.00</div>
      <div style="flex:1">
        <div style="font-size:11px;color:var(--text-muted)">Today</div>
        <div style="font-size:10px;color:var(--text-muted);margin-top:2px"><span id="cost-images">0</span> images · <span id="cost-sessions">0</span> sessions</div>
      </div>
      <div class="sparkline" id="cost-sparkline"></div>
    </div>

  </div>
</div>

<!-- ═══════ CENTER CANVAS ═══════ -->
<div class="panel" style="border-right:none;border-left:none;background:transparent;backdrop-filter:none;">
  <div style="display:flex;align-items:center;justify-content:space-between;padding:14px 16px 10px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.08em;color:var(--text-muted);">
    <span>Canvas</span>
    <span id="canvas-meta"></span>
  </div>
  <div class="panel-body" style="display:flex;flex-direction:column;gap:16px;">

    <!-- Main Preview -->
    <div style="position:relative;" id="canvas-wrapper" class="hidden">
      <button class="pin-btn" id="btn-pin-mode" title="Click to drop comment pins on image">📌 Pin Mode</button>
      <div class="canvas-wrap" id="canvas-wrap">
        <div class="canvas-area" id="canvas-main"></div>
        <div class="pin-layer" id="pin-layer"></div>
      </div>
    </div>
    <div class="canvas-area empty" id="canvas-main-empty">
      <div class="empty-state" id="canvas-empty">
        <div class="icon">🎨</div>
        <h3>Ready to create</h3>
        <p>Enter a prompt and click <b style="color:var(--primary);font-weight:600;">Generate</b> to create your first image.</p>
      </div>
    </div>

    <!-- Pin Toolbar -->
    <div id="pin-toolbar" class="hidden" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
      <span style="font-size:11px;color:var(--text-muted);">💡 Click anywhere on the image to add a comment pin. The AI will use pin locations to know which area to change.</span>
    </div>

    <!-- Refine Box -->
    <div id="refine-box" class="hidden" style="padding:14px;background:var(--bg-elevated);border-radius:var(--radius-sm);border:1px solid var(--border);">
      <label style="display:block;font-size:11px;font-weight:600;color:var(--text-muted);margin-bottom:6px;text-transform:uppercase;letter-spacing:0.05em;">Refine Selected</label>
      <textarea id="refine-changes" placeholder="What to change? e.g. ' warmer lighting, add a shadow beneath the product'" style="min-height:50px;margin-bottom:8px;"></textarea>
      <div style="display:flex;gap:8px;">
        <select id="refine-tier" style="flex:0 0 120px;">
          <option value="fast">Fast</option>
          <option value="balanced">Balanced</option>
          <option value="quality" selected>Quality</option>
        </select>
        <button class="btn accent" id="btn-refine" style="flex:1;">✨ Refine</button>
      </div>
    </div>

    <!-- Preview Grid -->
    <div class="preview-grid" id="output-grid"></div>

  </div>
</div>

<!-- ═══════ RIGHT PANEL ═══════ -->
<div class="panel" id="panel-right">
  <div class="panel-header"><span class="icon">&#128221;</span> Inspector</div>
  <div class="panel-body" id="inspector-body">
    <div class="empty-state" style="padding:20px 0;">
      <div class="icon" style="font-size:32px;">📋</div>
      <p style="font-size:13px;color:#94a3b8;line-height:1.6;">Generate an image to see details, QC results, and export options here.</p>
    </div>
    <!-- Pins Section (shown when image selected) -->
    <div id="inspector-pins" class="hidden" style="margin-top:12px;">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;">
        <span style="font-size:11px;font-weight:600;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.05em;">📝 Pins (<span id="pin-count">0</span>)</span>
        <button class="btn sm ghost" id="btn-clear-pins">Clear</button>
      </div>
      <div class="pin-list" id="pin-list"></div>
      <button class="btn primary" id="btn-refine-pins" style="width:100%;margin-top:8px;padding:10px;">🔧 Refine with Pins</button>
    </div>
  </div>
</div>

</div><!-- /main -->

</div><!-- /app -->

<!-- Toast -->
<div class="status-toast info" id="toast"></div>

<!-- Bottom Bar -->
<div class="bottom-bar">
  <div class="keys">
    <span><kbd>G</kbd> Generate</span>
    <span><kbd>C</kbd> Composite</span>
    <span><kbd>E</kbd> Export</span>
    <span><kbd>Q</kbd> QC</span>
  </div>
  <div><span style="color:var(--primary)">●</span> Creative Studio v5</div>
</div>

<script>
// ──────────────── STATE ──────────────────────────
let sessionId = 'sess_' + Math.random().toString(36).substr(2, 9);
let selectedImage = null;
let selectedTier = 'balanced';
let uploadedFiles = { ref: null, product: null, export: null, qc: null };
let currentMode = 'generate';

// ──────────────── HELPERS ────────────────────────
const $ = (q, el=document) => el.querySelector(q);
const $$ = (q, el=document) => Array.from(el.querySelectorAll(q));

function showToast(text, type='info') {
  const t = $('#toast');
  t.textContent = text;
  t.className = 'status-toast show ' + type;
  setTimeout(() => t.classList.remove('show'), 3000);
}

function setLoading(id, loading) {
  const btn = $(id);
  if (!btn) return;
  if (loading) { btn.dataset.orig = btn.textContent; btn.textContent = '⏳ Working...'; btn.disabled = true; }
  else { btn.textContent = btn.dataset.orig || btn.textContent; btn.disabled = false; }
}

function formatCost(n) { return '$' + (typeof n === 'number' ? n.toFixed(2) : '0.00'); }

async function post(url, body, isForm=false) {
  const opts = { method: 'POST' };
  if (isForm) opts.body = body;
  else { opts.headers = {'Content-Type':'application/json'}; opts.body = JSON.stringify(body); }
  const r = await fetch(url, opts);
  if (!r.ok) { const d = await r.json().catch(()=>({})); throw new Error(d.error || 'HTTP ' + r.status); }
  return r.json();
}

// ──────────────── MODE SWITCHING ────────────────
$$('#mode-tabs .nav-tab').forEach(tab => {
  tab.onclick = () => {
    $$('#mode-tabs .nav-tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    const mode = tab.dataset.mode;
    currentMode = mode;
    ['generate','composite','export','qc'].forEach(m => $(`#mode-${m}`).classList.add('hidden'));
    $(`#mode-${mode}`).classList.remove('hidden');
  };
});

// Keyboard shortcuts
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'TEXTAREA' || e.target.tagName === 'INPUT') return;
  const key = e.key.toLowerCase();
  if (key === 'g') $$('#mode-tabs .nav-tab')[0].click();
  if (key === 'c') { e.preventDefault(); $$('#mode-tabs .nav-tab')[1].click(); }
  if (key === 'e') { e.preventDefault(); $$('#mode-tabs .nav-tab')[2].click(); }
  if (key === 'q') { e.preventDefault(); $$('#mode-tabs .nav-tab')[3].click(); }
});

// ──────────────── DROPZONES ────────────────────
function setupDz(id, key) {
  const dz = $(id);
  if (!dz) return;
  const inp = dz.querySelector('input[type="file"]');
  dz.addEventListener('click', () => inp.click());
  dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('dragover'); });
  dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
  dz.addEventListener('drop', e => { e.preventDefault(); dz.classList.remove('dragover'); handleFile(e.dataTransfer.files[0], key, dz); });
  inp.addEventListener('change', e => handleFile(e.target.files[0], key, dz));
}
function handleFile(file, key, dz) {
  if (!file) return;
  uploadedFiles[key] = file;
  const reader = new FileReader();
  reader.onload = e => {
    dz.querySelector('.dz-inner').innerHTML = `<img src="${e.target.result}"><div style="margin-top:6px;font-size:12px;color:var(--text-muted)">${file.name}</div>`;
  };
  reader.readAsDataURL(file);
}
setupDz('#dz-ref', 'ref');
setupDz('#dz-product', 'product');
setupDz('#dz-export', 'export');
setupDz('#dz-qc', 'qc');

// Gen mode toggle
$('#gen-mode').onchange = e => {
  $('#variations-count-group').classList.toggle('hidden', e.target.value !== 'variations');
};

// Tier selection
$$('.model-card').forEach(card => {
  card.onclick = () => {
    $$('.model-card').forEach(c => c.classList.remove('selected'));
    card.classList.add('selected');
    selectedTier = card.dataset.tier;
  };
});

// Quick tags
$$('.tag').forEach(tag => {
  tag.onclick = () => {
    const input = $('#prompt-input');
    const text = tag.dataset.text;
    if (tag.classList.toggle('active')) {
      input.value = input.value.trim() ? input.value.trim() + ', ' + text : text;
    } else {
      input.value = input.value.replace(new RegExp(',?\\s*' + text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')), '').trim().replace(/,\s*$/, '');
    }
  };
});

// Export preset toggles
$$('.preset-card').forEach(p => {
  p.onclick = () => p.classList.toggle('checked');
});

// ──────────────── RENDER OUTPUTS ───────────────
function renderOutputs(images) {
  const grid = $('#output-grid');
  if (!images || !images.length) return;

  // Show canvas wrapper, hide empty state
  $('#canvas-main-empty').classList.add('hidden');
  $('#canvas-wrapper').classList.remove('hidden');

  // Show first image in canvas
  const first = images[0];
  $('#canvas-main').innerHTML = `<img src="${first.url}" alt="${first.name}" id="main-image" style="max-width:100%;display:block;">`;
  $('#canvas-meta').textContent = `${first.model} · ${first.name}`;

  // Show images in grid
  images.forEach((img, i) => {
    if ($(`[data-path="${img.path}"]`)) return; // skip dupes
    const el = document.createElement('div');
    el.className = 'preview-item';
    el.dataset.path = img.path;
    el.innerHTML = `
      <img src="${img.url}" alt="${img.name}" loading="lazy">
      <span class="cost-tag">${formatCost(img.cost)}</span>
      <div class="meta">
        <span>${img.name}</span>
        <span>${img.model?.split('-').pop() || ''}</span>
      </div>`;
    el.onclick = () => selectImage(img.path, img.url);
    grid.insertBefore(el, grid.firstChild);
  });

  $('#refine-box').classList.remove('hidden');
  $('#inspector-pins').classList.remove('hidden');
}

function selectImage(path, url) {
  selectedImage = path;
  $$('.preview-item').forEach(el => el.classList.remove('selected'));
  const item = $(`[data-path="${path}"]`);
  if (item) item.classList.add('selected');
  // Update main canvas
  $('#main-image')?.remove();
  $('#canvas-main').innerHTML = `<img src="${url}" id="main-image" alt="selected" style="max-width:100%;display:block;">`;
  // Load pins for this image
  loadPinsForImage(path);
}

// ──────────────── API CALLS ────────────────────
$('#btn-generate').onclick = async () => {
  const prompt = $('#prompt-input').value.trim();
  if (!prompt) { showToast('Enter a prompt first', 'error'); return; }
  setLoading('#btn-generate', true);
  try {
    const data = await post('/api/generate', {
      prompt, mode: $('#gen-mode').value, tier: selectedTier,
      aspect_ratio: $('#aspect-select').value, smart: $('#smart-check').checked,
      variations: parseInt($('#variations-count')?.value || 4), session_id: sessionId,
    });
    renderOutputs(data.images);
    showToast(data.message, 'success');
    refreshCosts();
  } catch(e) { showToast(e.message, 'error'); }
  setLoading('#btn-generate', false);
};

$('#btn-generate-top').onclick = () => $('#btn-generate').click();

$('#btn-composite').onclick = async () => {
  if (!uploadedFiles.product) { showToast('Upload product photo', 'error'); return; }
  const prompt = $('#composite-prompt').value.trim();
  if (!prompt) { showToast('Enter environment prompt', 'error'); return; }
  setLoading('#btn-composite', true);
  const fd = new FormData();
  fd.append('product', uploadedFiles.product);
  fd.append('prompt', prompt);
  fd.append('aspect_ratio', $('#composite-aspect').value);
  fd.append('session_id', sessionId);
  try {
    const data = await post('/api/composite', fd, true);
    renderOutputs(data.images);
    showToast(data.message, 'success');
    refreshCosts();
  } catch(e) { showToast(e.message, 'error'); }
  setLoading('#btn-composite', false);
};

$('#btn-export').onclick = async () => {
  if (!uploadedFiles.export) { showToast('Upload image first', 'error'); return; }
  const presets = $$('.preset-card.checked').map(el => el.dataset.preset).join(',');
  if (!presets) { showToast('Select at least one preset', 'error'); return; }
  setLoading('#btn-export', true);
  const fd = new FormData();
  fd.append('image', uploadedFiles.export);
  fd.append('presets', presets);
  fd.append('session_id', sessionId);
  try {
    const data = await post('/api/export', fd, true);
    renderOutputs(data.images);
    showToast(data.message, 'success');
    refreshCosts();
  } catch(e) { showToast(e.message, 'error'); }
  setLoading('#btn-export', false);
};

$('#btn-qc').onclick = async () => {
  if (!uploadedFiles.qc) { showToast('Upload image first', 'error'); return; }
  setLoading('#btn-qc', true);
  const fd = new FormData();
  fd.append('image', uploadedFiles.qc);
  try {
    const data = await post('/api/qc', fd, true);
    showToast(data.message, data.qc.quality_score >= 7 ? 'success' : 'error');
    renderQC(data.qc);
  } catch(e) { showToast(e.message, 'error'); }
  setLoading('#btn-qc', false);
};

$('#btn-refine').onclick = async () => {
  if (!selectedImage) { showToast('Select an image first', 'error'); return; }
  const changes = $('#refine-changes').value.trim();
  if (!changes) { showToast('Enter changes', 'error'); return; }
  setLoading('#btn-refine', true);
  try {
    const data = await post('/api/refine', {
      image_path: selectedImage, changes,
      tier: $('#refine-tier').value, session_id: sessionId,
    });
    renderOutputs(data.images);
    showToast(data.message, 'success');
    refreshCosts();
  } catch(e) { showToast(e.message, 'error'); }
  setLoading('#btn-refine', false);
};

// ──────────────── QC RENDER ──────────────────────
function renderQC(qc) {
  const el = $('#qc-results');
  const score = qc.quality_score || 0;
  const status = score >= 7 ? 'pass' : score >= 4 ? '' : 'fail';
  el.innerHTML = `
    <div style="margin-top:16px">
      <div class="qc-score-ring" style="border-color:${score>=7?'var(--success)':score>=4?'var(--accent)':'var(--danger)'};">
        <span class="score" style="color:${score>=7?'var(--success)':score>=4?'var(--accent)':'var(--danger)'}">${score}</span>
      </div>
      <div style="text-align:center;font-size:11px;color:var(--text-muted);margin-bottom:12px;">/10</div>
      <div class="qc-grid">
        <div class="qc-item ${qc.floating_products?'fail':'pass'}"><div class="status-icon">${qc.floating_products?'✗':'✓'}</div><div class="label">Floating</div></div>
        <div class="qc-item ${qc.garbled_text?'fail':'pass'}"><div class="status-icon">${qc.garbled_text?'✗':'✓'}</div><div class="label">Text</div></div>
        <div class="qc-item ${qc.detached_shadows?'fail':'pass'}"><div class="status-icon">${qc.detached_shadows?'✗':'✓'}</div><div class="label">Shadows</div></div>
        <div class="qc-item ${qc.fake_products?'fail':'pass'}"><div class="status-icon">${qc.fake_products?'✗':'✓'}</div><div class="label">Fake Products</div></div>
        <div class="qc-item ${!qc.readable_labels?'fail':'pass'}"><div class="status-icon">${!qc.readable_labels?'✗':'✓'}</div><div class="label">Labels</div></div>
      </div>
      ${qc.issues?.length ? '<ul style="margin-top:12px;font-size:12px;color:var(--text-muted);list-style:none">' + qc.issues.map(i=>`<li style="padding:4px 0;border-bottom:1px solid var(--border)">⚠ ${i}</li>`).join('') + '</ul>' : ''}
    </div>`;
}

// ──────────────── COSTS ──────────────────────────
async function refreshCosts() {
  try {
    const c = await fetch('/api/costs').then(r => r.json());
    $('#cost-amount').textContent = formatCost(c.total);
    $('#cost-images').textContent = c.image_count;
    $('#cost-sessions').textContent = c.session_count;
    // Sparkline
    const bars = Object.values(c.by_date || {}).map(v => Math.max(1, Math.round(v * 50)));
    $('#cost-sparkline').innerHTML = bars.map(h => `<div class="bar" style="height:${Math.min(20, h)}px"></div>`).join('') || '<div style="font-size:10px;color:var(--text-muted)">No data</div>';
  } catch(e) { console.error(e); }
}

// ──────────────── INIT ───────────────────────────
refreshCosts();

// Theme toggle
let dark = true;
$('#btn-theme').onclick = () => {
  dark = !dark;
  document.body.style.background = dark ? 'var(--bg)' : '#f5f7fa';
  document.body.style.color = dark ? 'var(--text)' : '#1a1a2e';
};

// ─── PIN ANNOTATION SYSTEM ────────────────────────────
let pinMode = false;
let activePins = [];
let tempPin = null;

function setPinMode(on) {
  pinMode = on;
  const btn = $('#btn-pin-mode');
  const layer = $('#pin-layer');
  const toolbar = $('#pin-toolbar');
  if (on) { btn.classList.add('active'); btn.textContent = '📌 Pin Mode ON'; layer.classList.add('active'); toolbar.classList.remove('hidden'); showToast('Click anywhere on the image to drop a comment pin', 'info'); }
  else { btn.classList.remove('active'); btn.textContent = '📌 Pin Mode'; layer.classList.remove('active'); toolbar.classList.add('hidden'); cancelTempPin(); }
}

$('#btn-pin-mode').onclick = () => setPinMode(!pinMode);

function cancelTempPin() { if (tempPin) { tempPin.remove(); tempPin = null; } }

function showPinConfirm(vx, vy, nx, ny) {
  cancelTempPin();
  const wrap = $('#canvas-wrap');
  if (!wrap) return;
  const el = document.createElement('div');
  el.className = 'pin-confirm';
  el.style.left = (vx - 100) + 'px';
  el.style.top = (vy - 70) + 'px';
  el.innerHTML = `<input id="pin-text-input" placeholder="What to change here?" style="flex:1;padding:6px 8px;font-size:12px;"><button class="btn primary sm" id="pin-ok">Add</button><button class="btn ghost sm" id="pin-cancel">✕</button>`;
  wrap.appendChild(el);
  tempPin = el;
  setTimeout(() => $('#pin-text-input')?.focus(), 50);
  $('#pin-ok').onclick = async () => { const t = $('#pin-text-input')?.value?.trim(); if (!t) return; cancelTempPin(); await addPin(selectedImage, nx, ny, t); };
  $('#pin-cancel').onclick = () => cancelTempPin();
  $('#pin-text-input')?.addEventListener('keydown', (e) => { if (e.key === 'Enter') $('#pin-ok').click(); if (e.key === 'Escape') cancelTempPin(); });
}

$('#pin-layer').onclick = (e) => {
  if (!pinMode) return;
  e.stopPropagation();
  const img = $('#main-image');
  if (!img) return;
  const r = img.getBoundingClientRect();
  const nx = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
  const ny = Math.min(1, Math.max(0, (e.clientY - r.top) / r.height));
  showPinConfirm(e.clientX, e.clientY, nx, ny);
};

async function addPin(imagePath, x, y, text) {
  if (!imagePath) { showToast('Select an image first', 'error'); return; }
  try {
    const d = await post('/api/pins', { image_path: imagePath, x, y, text });
    activePins = d.pins || [];
    renderPinsOnImage();
    renderPinList();
    showToast('Pin added', 'success');
  } catch(e) { showToast(e.message, 'error'); }
}

async function loadPinsForImage(imagePath) {
  if (!imagePath) { activePins = []; renderPinsOnImage(); renderPinList(); return; }
  try {
    const d = await (await fetch('/api/pins/' + encodeURIComponent(imagePath))).json();
    activePins = d.pins || [];
    renderPinsOnImage();
    renderPinList();
  } catch(e) { activePins = []; }
}

async function deletePin(pid) {
  if (!selectedImage || !pid) return;
  try { const d = await (await fetch('/api/pins/' + encodeURIComponent(selectedImage) + '/' + pid, { method: 'DELETE' })).json(); activePins = d.pins || []; renderPinsOnImage(); renderPinList(); } catch(e) {}
}

async function clearPins() {
  if (!selectedImage) return;
  try { const d = await (await fetch('/api/pins/' + encodeURIComponent(selectedImage), { method: 'DELETE' })).json(); activePins = d.pins || []; renderPinsOnImage(); renderPinList(); showToast('All pins cleared', 'info'); } catch(e) {}
}

function renderPinsOnImage() {
  const layer = $('#pin-layer');
  const img = $('#main-image');
  if (!layer || !img) return;
  layer.innerHTML = '';
  const r = img.getBoundingClientRect();
  const pr = img.parentElement.getBoundingClientRect();
  const ox = r.left - pr.left;
  const oy = r.top - pr.top;
  activePins.forEach(p => {
    const m = document.createElement('div');
    m.className = 'pin-marker';
    m.style.left = (ox + p.x * r.width) + 'px';
    m.style.top = (oy + p.y * r.height) + 'px';
    m.innerHTML = `<div class="tip">${escapeHtml(p.text)}</div><div class="dot" style="background:${pinMode?'var(--primary)':'#06D6A0'}"></div>`;
    m.onclick = (e) => { e.stopPropagation(); deletePin(p.id); };
    layer.appendChild(m);
  });
}

function renderPinList() {
  const list = $('#pin-list');
  const count = $('#pin-count');
  if (!list) return;
  count.textContent = activePins.length;
  if (!activePins.length) { list.innerHTML = '<div style="font-size:12px;color:var(--text-muted);padding:8px 0;">No pins. Enable Pin Mode and click the image to add area-specific feedback.</div>'; return; }
  list.innerHTML = activePins.map(p => `<div class="pin-row"><span class="region">${regionLabel(p.x,p.y)}</span><span class="text">${escapeHtml(p.text)}</span><span class="del" data-pid="${p.id}">✕</span></div>`).join('');
  list.querySelectorAll('.del').forEach(el => { el.onclick = () => deletePin(el.dataset.pid); });
}

function regionLabel(x, y) {
  const h = y < 0.33 ? 'top' : y < 0.66 ? 'mid' : 'bot';
  const w = x < 0.33 ? 'left' : x < 0.66 ? 'ctr' : 'right';
  if (h === 'mid' && w === 'ctr') return 'center';
  return h + '-' + w;
}
function escapeHtml(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

$('#btn-clear-pins').onclick = clearPins;

$('#btn-refine-pins').onclick = async () => {
  if (!selectedImage) { showToast('Select an image first', 'error'); return; }
  if (!activePins.length) { showToast('Add at least one pin first', 'error'); return; }
  const tier = $('#refine-tier')?.value || 'quality';
  setLoading('#btn-refine-pins', true);
  try {
    const data = await post('/api/refine', {
      image_path: selectedImage, changes: '',
      pins: activePins.map(p => ({ x: p.x, y: p.y, text: p.text })),
      tier, session_id: sessionId,
    });
    renderOutputs(data.images);
    showToast(data.message, 'success');
    refreshCosts();
  } catch(e) { showToast(e.message, 'error'); }
  setLoading('#btn-refine-pins', false);
};

window.addEventListener('resize', () => { if (activePins.length) renderPinsOnImage(); });
</script>
</body>
</html>

'''

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

# ── API Routes ──────────────────────────────────────────────────────────

@app.route("/api/generate", methods=["POST"])
def api_generate():
    data = request.json or {}
    prompt = data.get("prompt", "").strip()
    if not prompt:
        return jsonify({"error": "Prompt required"}), 400

    mode = data.get("mode", "direct")
    tier = data.get("tier", "balanced")
    aspect = data.get("aspect_ratio", "16:9")
    smart = data.get("smart", True)
    variations = int(data.get("variations", 4))
    session_id = data.get("session_id", new_session_id())

    images = run_cli_generate(prompt, mode, tier, aspect, smart, variations=variations)
    for img in images:
        if "error" not in img:
            add_entry(session_id, {
                "type": mode, "prompt": prompt[:100], "cost": img.get("cost", 0),
                "image_url": img.get("url", ""), "model": img.get("model", ""),
                "note": f"{img.get('name', '')} ({img.get('model', '')})"
            })

    costs = load_costs()
    costs["session_count"] = len(list(SESSIONS_DIR.glob("*.json")))
    save_costs(costs)

    return jsonify({"message": f"Generated {len(images)} image(s)", "images": images, "session_id": session_id})


@app.route("/api/composite", methods=["POST"])
def api_composite():
    if "product" not in request.files:
        return jsonify({"error": "Product image required"}), 400
    f = request.files["product"]
    prompt = request.form.get("prompt", "").strip()
    aspect = request.form.get("aspect_ratio", "16:9")
    session_id = request.form.get("session_id", new_session_id())
    if not prompt:
        return jsonify({"error": "Prompt required"}), 400

    tmp_dir = DATA_DIR / "uploads"
    tmp_dir.mkdir(exist_ok=True)
    product_path = tmp_dir / f"product_{int(time.time())}_{f.filename}"
    f.save(str(product_path))

    images = run_cli_composite(prompt, str(product_path), aspect)
    for img in images:
        add_entry(session_id, {
            "type": "composite", "prompt": prompt[:100], "cost": img.get("cost", 0),
            "image_url": img.get("url", ""), "model": img.get("model", ""),
            "note": img.get("name", "")
        })

    return jsonify({"message": "Composite generated", "images": images, "session_id": session_id})


@app.route("/api/export", methods=["POST"])
def api_export():
    if "image" not in request.files:
        return jsonify({"error": "Image required"}), 400
    f = request.files["image"]
    presets = request.form.get("presets", "")
    session_id = request.form.get("session_id", new_session_id())
    if not presets:
        return jsonify({"error": "Presets required"}), 400

    tmp_dir = DATA_DIR / "uploads"
    tmp_dir.mkdir(exist_ok=True)
    src_path = tmp_dir / f"export_{int(time.time())}_{f.filename}"
    f.save(str(src_path))

    images = run_cli_export(str(src_path), presets)
    for img in images:
        add_entry(session_id, {
            "type": "export", "cost": 0, "image_url": img.get("url", ""),
            "model": "PIL", "note": img.get("name", "")
        })

    return jsonify({"message": f"Exported to {len(images)} formats", "images": images, "session_id": session_id})


@app.route("/api/qc", methods=["POST"])
def api_qc():
    if "image" not in request.files:
        return jsonify({"error": "Image required"}), 400
    f = request.files["image"]
    tmp_dir = DATA_DIR / "uploads"
    tmp_dir.mkdir(exist_ok=True)
    img_path = tmp_dir / f"qc_{int(time.time())}_{f.filename}"
    f.save(str(img_path))

    qc = run_cli_qc(str(img_path))
    return jsonify({"message": f"QC Score: {qc['quality_score']}/10", "qc": qc})


@app.route("/api/refine", methods=["POST"])
def api_refine():
    data = request.json or {}
    image_path = data.get("image_path", "")
    changes = data.get("changes", "").strip()
    pins = data.get("pins", [])
    tier = data.get("tier", "quality")
    session_id = data.get("session_id", new_session_id())

    # Build spatial prompt from pins + user text
    pin_text = build_pin_prompt(pins) if pins else ""
    if pin_text and changes:
        full_changes = f"{changes}. Also: {pin_text}"
    elif pin_text:
        full_changes = pin_text
    elif changes:
        full_changes = changes
    else:
        return jsonify({"error": "changes or pins required"}), 400

    images = run_cli_refine(image_path, full_changes, tier)
    for img in images:
        add_entry(session_id, {
            "type": "refine", "cost": img.get("cost", 0), "image_url": img.get("url", ""),
            "model": img.get("model", ""), "note": full_changes[:200]
        })

    return jsonify({"message": "Refined", "images": images, "session_id": session_id})


# ── Pin Annotation Routes ───────────────────────────────────────────────

@app.route("/api/pins", methods=["POST"])
def api_pins_add():
    data = request.json or {}
    image_path = data.get("image_path", "")
    x = float(data.get("x", 0.5))
    y = float(data.get("y", 0.5))
    text = data.get("text", "").strip()
    if not image_path or not text:
        return jsonify({"error": "image_path and text required"}), 400
    pins = load_pins(image_path)
    pins.append({"id": pin_id(), "x": x, "y": y, "text": text, "time": now_str()})
    save_pins(image_path, pins)
    return jsonify({"pins": pins})


@app.route("/api/pins/<path:image_path>", methods=["GET"])
def api_pins_get(image_path):
    if image_path and not image_path.startswith('/'):
        image_path = '/' + image_path
    return jsonify({"pins": load_pins(image_path)})


@app.route("/api/pins/<path:image_path>/<pin_id>", methods=["DELETE"])
def api_pins_delete(image_path, pin_id):
    if image_path and not image_path.startswith('/'):
        image_path = '/' + image_path
    pins = [p for p in load_pins(image_path) if p.get("id") != pin_id]
    save_pins(image_path, pins)
    return jsonify({"pins": pins})


@app.route("/api/pins/<path:image_path>", methods=["DELETE"])
def api_pins_clear(image_path):
    if image_path and not image_path.startswith('/'):
        image_path = '/' + image_path
    save_pins(image_path, [])
    return jsonify({"pins": []})


@app.route("/api/sessions", methods=["GET"])
def api_sessions():
    sessions = []
    for p in sorted(SESSIONS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        data = load_json(p)
        sessions.append({
            "id": data.get("id", p.stem),
            "created_at": data.get("created_at", ""),
            "entries": data.get("entries", []),
            "cost": sum(e.get("cost", 0) for e in data.get("entries", [])),
        })
    return jsonify({"sessions": sessions})


@app.route("/api/session/<session_id>", methods=["GET"])
def api_session_get(session_id):
    data = load_session(session_id)
    entries = []
    for e in data.get("entries", []):
        e2 = dict(e)
        e2["image_url"] = e.get("image_url", "")
        entries.append(e2)
    return jsonify({"id": session_id, "entries": entries, "created_at": data.get("created_at", "")})


@app.route("/api/costs", methods=["GET"])
def api_costs():
    costs = load_costs()
    costs["session_count"] = len(list(SESSIONS_DIR.glob("*.json")))
    return jsonify(costs)


@app.route("/image/<path:subpath>")
def serve_image(subpath):
    parts = subpath.split("/")
    # Navigate safely
    target = OUTPUT_DIR
    for part in parts:
        target = target / part
    if target.exists() and target.is_file():
        return send_from_directory(str(target.parent), target.name)
    return jsonify({"error": "Not found"}), 404


# ─── Main ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5173)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    print(f"🎨 Creative Studio Web App running at http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)
