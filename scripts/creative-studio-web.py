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
    if p.exists():
        return f"/image/{p.parent.name}/{p.name}"
    return ""

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
                "-v", str(variations)]
    else:
        args = ["bash", str(Path(__file__).parent.parent / "launch.sh"), "direct",
                "--prompt", prompt, "--tier", tier, "--aspect-ratio", aspect]
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

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en" class="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Creative Studio — AI Image Generation</title>
<style>
:root { --bg:#0f172a; --surface:#1e293b; --border:#334155; --text:#e2e8f0; --muted:#94a3b8; --primary:#60a5fa; --accent:#f59e0b; --success:#34d399; --danger:#f87171; --font:system-ui,-apple-system,sans-serif; }
html.light { --bg:#f8fafc; --surface:#fff; --border:#e2e8f0; --text:#0f172a; --muted:#64748b; }
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:var(--font);background:var(--bg);color:var(--text);line-height:1.5}
.container{max-width:1400px;margin:0 auto;padding:1rem}
header{display:flex;justify-content:space-between;align-items:center;padding:1rem 0;border-bottom:1px solid var(--border);margin-bottom:1.5rem}
header h1{font-size:1.5rem;display:flex;align-items:center;gap:0.5rem}
header h1 span{color:var(--accent)}
.actions{display:flex;gap:0.75rem}
.btn{padding:0.5rem 1rem;border-radius:6px;border:1px solid var(--border);background:var(--surface);color:var(--text);cursor:pointer;font-size:0.875rem;transition:all 0.15s}
.btn:hover{border-color:var(--primary)}
.btn.primary{background:var(--primary);border-color:var(--primary);color:#fff}
.btn.accent{background:var(--accent);border-color:var(--accent);color:#0f172a;font-weight:600}
.btn.sm{padding:0.35rem 0.75rem;font-size:0.8125rem}
.grid{display:grid;grid-template-columns:380px 1fr;gap:1.5rem}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
.panel{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:1rem}
.panel h2{font-size:1rem;margin-bottom:0.75rem;color:var(--muted);text-transform:uppercase;letter-spacing:0.05em}
.form-group{margin-bottom:0.8rem}
label{display:block;font-size:0.8125rem;color:var(--muted);margin-bottom:0.35rem}
input,textarea,select{width:100%;padding:0.5rem;border-radius:6px;border:1px solid var(--border);background:var(--bg);color:var(--text);font-size:0.875rem}
textarea{resize:vertical;min-height:80px;font-family:var(--font)}
.tabs{display:flex;gap:0.25rem;margin-bottom:1rem;border-bottom:1px solid var(--border)}
.tab{cursor:pointer;padding:0.5rem 1rem;border-bottom:2px solid transparent;color:var(--muted);font-size:0.875rem}
.tab.active{color:var(--primary);border-bottom-color:var(--primary)}
.tab-content{display:none}
.tab-content.active{display:block}
.dropzone{border:2px dashed var(--border);border-radius:8px;padding:2rem;text-align:center;cursor:pointer;transition:all 0.15s;margin-bottom:1rem}
.dropzone:hover,.dropzone.dragover{border-color:var(--primary);background:rgba(96,165,250,0.05)}
.dropzone img{max-width:100%;max-height:160px;border-radius:6px;margin-top:0.5rem}
.preview-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:0.75rem;margin-top:0.75rem}
.preview-item{position:relative;border:1px solid var(--border);border-radius:6px;overflow:hidden;cursor:pointer}
.preview-item img{width:100%;height:auto;display:block}
.preview-item.selected{outline:2px solid var(--primary);outline-offset:2px}
.preview-item .badge{position:absolute;top:4px;right:4px;background:rgba(0,0,0,0.75);color:#fff;font-size:0.7rem;padding:0.15rem 0.4rem;border-radius:4px}
.preview-item .actions{position:absolute;bottom:0;left:0;right:0;background:rgba(0,0,0,0.8);opacity:0;transition:opacity 0.15s;padding:0.25rem;display:flex;gap:0.3rem}
.preview-item:hover .actions{opacity:1}
.status{font-size:0.8125rem;color:var(--muted);padding:0.5rem;border-radius:6px;background:rgba(0,0,0,0.2);display:none}
.status.show{display:block}
.status.error{color:var(--danger);background:rgba(248,113,113,0.1)}
.status.success{color:var(--success);background:rgba(52,211,153,0.1)}
.cost-bar{display:flex;gap:1rem;margin-top:1rem;font-size:0.8125rem;color:var(--muted)}
.cost-bar strong{color:var(--accent)}
.timeline{margin-top:1rem}
.timeline-item{display:flex;gap:0.75rem;padding:0.6rem;border-bottom:1px solid var(--border);font-size:0.8125rem}
.timeline-item:last-child{border-bottom:none}
.timeline-item img{width:48px;height:48px;object-fit:cover;border-radius:4px}
.timeline-item small{color:var(--muted)}
.session-row{display:flex;justify-content:space-between;align-items:center;padding:0.5rem 0;border-bottom:1px solid var(--border);cursor:pointer}
.session-row:hover{color:var(--primary)}
.export-list{display:flex;flex-wrap:wrap;gap:0.5rem;margin-top:0.5rem}
.export-label{font-size:0.75rem;padding:0.2rem 0.5rem;border-radius:4px;background:var(--bg);border:1px solid var(--border);color:var(--muted)}
.export-label.checked{background:var(--primary);color:#fff;border-color:var(--primary)}
.hidden{display:none!important}
#dropzone-product img,#dropzone-ref img{max-height:120px}
#canvas-area{position:relative;min-height:300px;background:#0a0f1a;border:1px dashed var(--border);border-radius:8px;display:flex;align-items:center;justify-content:center;color:var(--muted)}
#canvas-area img{max-width:90%;max-height:300px;border-radius:4px}
.qc-grid{display:grid;grid-template-columns:1fr 1fr;gap:0.5rem;font-size:0.8125rem;margin-top:0.5rem}
.qc-pass{color:var(--success)}.qc-fail{color:var(--danger)}
</style>
</head>
<body>
<div class="container">
<header>
<h1>🎨 Creative <span>Studio</span></h1>
<div class="actions">
<button class="btn sm" id="theme-toggle">🌙</button>
</div>
</header>
<div class="grid">
<div class="left-col">
<div class="tabs">
<div class="tab active" data-tab="generate">Generate</div>
<div class="tab" data-tab="composite">Composite</div>
<div class="tab" data-tab="export">Export</div>
<div class="tab" data-tab="qc">QC</div>
<div class="tab" data-tab="history">Sessions</div>
</div>

<div class="tab-content active" id="tab-generate">
<div class="panel">
<h2>🖼️ Generation</h2>
<div class="form-group">
<label>Prompt</label>
<textarea id="prompt-input" placeholder="Your exact prompt..."></textarea>
</div>
<div class="form-group">
<label>Mode</label>
<select id="mode-select">
<option value="direct">Direct (one-shot)</option>
<option value="variations">Variations (4 pack)</option>
</select>
</div>
<div class="form-group hidden" id="variations-opts">
<label>Variation Count</label>
<input type="number" id="variations-count" value="4" min="1" max="8">
</div>
<div class="form-group">
<label>Tier</label>
<select id="tier-select">
<option value="fast">Fast (~$0.07)</option>
<option value="balanced" selected>Balanced (~$0.07)</option>
<option value="quality">Quality (~$0.20)</option>
<option value="ultra">Ultra (~$0.40)</option>
</select>
</div>
<div class="form-group">
<label>Aspect Ratio</label>
<select id="aspect-select">
<option value="16:9">16:9 (widescreen)</option>
<option value="1:1">1:1 (square)</option>
<option value="4:5">4:5 (Instagram)</option>
<option value="9:16">9:16 (stories)</option>
<option value="3:2">3:2 (print)</option>
</select>
</div>
<div class="form-group">
<label>Reference Image</label>
<div class="dropzone" id="dropzone-ref">
<div class="dz-text">Drop reference image here (optional)</div>
<input type="file" id="ref-file" accept="image/*" style="display:none">
</div>
</div>
<div class="form-group">
<label><input type="checkbox" id="smart-check" checked> Smart prompt enhancement</label>
</div>
<button class="btn primary" id="btn-generate" style="width:100%">Generate</button>
<div class="status" id="status-generate"></div>
</div>
<div class="panel" style="margin-top:1rem" id="refine-panel">
<h2>✏️ Refine Selected</h2>
<div class="form-group">
<label>Changes</label>
<textarea id="refine-changes" placeholder="What to change..."></textarea>
</div>
<div class="form-group">
<label>Tier</label>
<select id="refine-tier">
<option value="fast">Fast</option>
<option value="balanced">Balanced</option>
<option value="quality" selected>Quality</option>
</select>
</div>
<button class="btn accent" id="btn-refine" style="width:100%">Refine Selected</button>
<div class="status" id="status-refine"></div>
</div>
</div>

<div class="tab-content" id="tab-composite">
<div class="panel">
<h2>🔧 Composite (Zero Hallucinations)</h2>
<p style="font-size:0.8125rem;color:var(--muted);margin-bottom:0.75rem">AI generates only the background. Your real product is composited on top.</p>
<div class="form-group">
<label>Product Photo</label>
<div class="dropzone" id="dropzone-product">
<div class="dz-text">Drop your product photo here</div>
<input type="file" id="product-file" accept="image/*" style="display:none">
</div>
</div>
<div class="form-group">
<label>Environment Prompt</label>
<textarea id="composite-prompt" placeholder="Empty clean wooden retail shelves in a premium store. No products, no bottles..."></textarea>
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
<button class="btn primary" id="btn-composite" style="width:100%">Generate Composite</button>
<div class="status" id="status-composite"></div>
</div>
</div>

<div class="tab-content" id="tab-export">
<div class="panel">
<h2>📦 Export Multi-Format</h2>
<div class="form-group">
<label>Source Image</label>
<div class="dropzone" id="dropzone-export">
<div class="dz-text">Drop image to export</div>
<input type="file" id="export-file" accept="image/*" style="display:none">
</div>
</div>
<p style="font-size:0.8125rem;color:var(--muted);margin-bottom:0.5rem">Select platforms:</p>
<div class="export-list" id="export-presets">
<span class="export-label checked" data-preset="amazon">Amazon</span>
<span class="export-label checked" data-preset="shopify">Shopify</span>
<span class="export-label checked" data-preset="meta-feed">Meta Feed</span>
<span class="export-label" data-preset="meta-stories">Meta Stories</span>
<span class="export-label checked" data-preset="web-hero">Web Hero</span>
<span class="export-label" data-preset="pinterest">Pinterest</span>
<span class="export-label" data-preset="print-dpi">Print 300DPI</span>
</div>
<button class="btn primary" id="btn-export" style="width:100%;margin-top:1rem">Export</button>
<div class="status" id="status-export"></div>
</div>
</div>

<div class="tab-content" id="tab-qc">
<div class="panel">
<h2>🔍 Quality Check</h2>
<div class="form-group">
<label>Image to Inspect</label>
<div class="dropzone" id="dropzone-qc">
<div class="dz-text">Drop image to check</div>
<input type="file" id="qc-file" accept="image/*" style="display:none">
</div>
</div>
<button class="btn primary" id="btn-qc" style="width:100%">Run QC</button>
<div class="status" id="status-qc"></div>
<div id="qc-results"></div>
</div>
</div>

<div class="tab-content" id="tab-history">
<div class="panel">
<h2>📁 Sessions</h2>
<div id="sessions-list"><div style="text-align:center;color:var(--muted);padding:2rem">Loading sessions...</div></div>
</div>
<div class="panel" style="margin-top:1rem">
<h2>💰 Cost Tracker</h2>
<div id="cost-display" style="text-align:center;padding:1rem;color:var(--muted)">Loading costs...</div>
</div>
</div>
</div>

<div class="right-col">
<div class="panel" id="output-panel" style="min-height:420px">
<h2>🖼️ Output Canvas</h2>
<div id="status-main" class="status" style="margin-bottom:0.5rem"></div>
<div id="canvas-area">
<div style="text-align:center">
<p>Generated images appear here</p>
<p style="font-size:0.8125rem;color:var(--muted)">Select a generation mode on the left</p>
</div>
</div>
<div class="preview-grid" id="output-grid"></div>
</div>
<div class="panel" id="session-timeline" style="display:none">
<h2>⏱️ Timeline</h2>
<div class="timeline" id="timeline-content"></div>
</div>
</div>
</div>
</div>

<script>
let sessionId = 'sess_' + Math.random().toString(36).substr(2, 9);
let selectedImage = null;
let uploadedFiles = { ref: null, product: null, export: null, qc: null };
const $ = (q, el=document) => el.querySelector(q);
const $$ = (q, el=document) => [...el.querySelectorAll(q)];

$('#theme-toggle').onclick = () => {
  document.documentElement.classList.toggle('light');
  document.documentElement.classList.toggle('dark');
};

$$('.tab').forEach(t => t.onclick = () => {
  $$('.tab').forEach(x => x.classList.remove('active'));
  $$('.tab-content').forEach(x => x.classList.remove('active'));
  t.classList.add('active');
  $(`#tab-${t.dataset.tab}`).classList.add('active');
});

$('#mode-select').onchange = (e) => {
  $('#variations-opts').classList.toggle('hidden', e.target.value !== 'variations');
};

function setupDropzone(id, key) {
  const dz = $(`#${id}`);
  const inp = dz.querySelector('input[type="file"]');
  dz.addEventListener('click', () => inp.click());
  dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('dragover'); });
  dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
  dz.addEventListener('drop', e => { e.preventDefault(); dz.classList.remove('dragover'); handleFiles(e.dataTransfer.files, key); });
  inp.addEventListener('change', e => handleFiles(e.target.files, key));
}
function handleFiles(files, key) {
  if (!files.length) return;
  const f = files[0]; uploadedFiles[key] = f;
  const dz = $(`#dropzone-${key}`);
  const reader = new FileReader();
  reader.onload = e => {
    dz.querySelector('.dz-text').innerHTML = `<img src="${e.target.result}"><br><small>${f.name} (${(f.size/1024).toFixed(0)}KB)</small>`;
  };
  reader.readAsDataURL(f);
}
setupDropzone('dropzone-ref', 'ref');
setupDropzone('dropzone-product', 'product');
setupDropzone('dropzone-export', 'export');
setupDropzone('dropzone-qc', 'qc');

$$('#export-presets .export-label').forEach(el => {
  el.onclick = () => el.classList.toggle('checked');
});

function showStatus(id, text, type='') {
  const el = $(`#${id}`);
  el.textContent = text; el.className = 'status show ' + type;
  if (!text) el.classList.remove('show');
}

async function post(url, body, isForm=false) {
  const opts = { method: 'POST' };
  if (isForm) { opts.body = body; }
  else { opts.headers = {'Content-Type':'application/json'}; opts.body = JSON.stringify(body); }
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error((await r.json()).error || `HTTP ${r.status}`);
  return r.json();
}

$('#btn-generate').onclick = async () => {
  const prompt = $('#prompt-input').value.trim();
  if (!prompt) return showStatus('status-generate', 'Enter a prompt', 'error');
  showStatus('status-generate', 'Generating...');
  try {
    const data = await post('/api/generate', {
      prompt, mode: $('#mode-select').value, tier: $('#tier-select').value,
      aspect_ratio: $('#aspect-select').value, smart: $('#smart-check').checked,
      variations: parseInt($('#variations-count').value) || 4, session_id: sessionId,
    });
    showStatus('status-generate', data.message, 'success');
    renderOutputs(data.images);
    refreshSessions();
  } catch(e) { showStatus('status-generate', e.message, 'error'); }
};

$('#btn-composite').onclick = async () => {
  if (!uploadedFiles.product) return showStatus('status-composite', 'Upload product photo', 'error');
  const prompt = $('#composite-prompt').value.trim();
  if (!prompt) return showStatus('status-composite', 'Enter environment prompt', 'error');
  showStatus('status-composite', 'Generating composite...');
  const fd = new FormData();
  fd.append('product', uploadedFiles.product);
  fd.append('prompt', prompt);
  fd.append('aspect_ratio', $('#composite-aspect').value);
  fd.append('session_id', sessionId);
  try {
    const data = await post('/api/composite', fd, true);
    showStatus('status-composite', data.message, 'success');
    renderOutputs(data.images);
    refreshSessions();
  } catch(e) { showStatus('status-composite', e.message, 'error'); }
};

$('#btn-export').onclick = async () => {
  if (!uploadedFiles.export) return showStatus('status-export', 'Upload image first', 'error');
  const presets = $$('#export-presets .export-label.checked').map(el => el.dataset.preset).join(',');
  if (!presets) return showStatus('status-export', 'Select at least one preset', 'error');
  showStatus('status-export', 'Exporting...');
  const fd = new FormData();
  fd.append('image', uploadedFiles.export);
  fd.append('presets', presets);
  fd.append('session_id', sessionId);
  try {
    const data = await post('/api/export', fd, true);
    showStatus('status-export', data.message, 'success');
    renderOutputs(data.images);
    refreshSessions();
  } catch(e) { showStatus('status-export', e.message, 'error'); }
};

$('#btn-qc').onclick = async () => {
  if (!uploadedFiles.qc) return showStatus('status-qc', 'Upload image first', 'error');
  showStatus('status-qc', 'Running QC...');
  const fd = new FormData();
  fd.append('image', uploadedFiles.qc);
  try {
    const data = await post('/api/qc', fd, true);
    showStatus('status-qc', data.message, data.qc.quality_score >= 7 ? 'success' : 'error');
    renderQC(data.qc);
  } catch(e) { showStatus('status-qc', e.message, 'error'); }
};

$('#btn-refine').onclick = async () => {
  if (!selectedImage) return showStatus('status-refine', 'Select an image first', 'error');
  const changes = $('#refine-changes').value.trim();
  if (!changes) return showStatus('status-refine', 'Enter changes', 'error');
  showStatus('status-refine', 'Refining...');
  try {
    const data = await post('/api/refine', {
      image_path: selectedImage, changes,
      tier: $('#refine-tier').value, session_id: sessionId,
    });
    showStatus('status-refine', data.message, 'success');
    renderOutputs(data.images);
    refreshSessions();
  } catch(e) { showStatus('status-refine', e.message, 'error'); }
};

function renderOutputs(images) {
  const grid = $('#output-grid');
  const area = $('#canvas-area');
  area.innerHTML = '';
  if (!images || !images.length) return;
  images.forEach((img, i) => {
    const el = document.createElement('div');
    el.className = 'preview-item';
    el.innerHTML = `<img src="${img.url}"><span class="badge">${img.name}</span>
      <div class="actions">
        <button class="btn sm" onclick="downloadImage('${img.url}', '${img.name}')">⬇️</button>
        <button class="btn sm" onclick="selectImage('${img.path}', this)">✓ Select</button>
      </div>`;
    grid.appendChild(el);
    if (i === 0) { area.innerHTML = `<img src="${img.url}" style="max-height:280px">`; }
  });
}
window.downloadImage = (url, name) => {
  const a = document.createElement('a'); a.href = url; a.download = name; a.click();
};
window.selectImage = (path, btn) => {
  selectedImage = path;
  $$('.preview-item').forEach(el => el.classList.remove('selected'));
  btn.closest('.preview-item').classList.add('selected');
  showStatus('status-main', `Selected: ${path.split('/').pop()}`, 'success');
};

function renderQC(qc) {
  const el = $('#qc-results');
  el.innerHTML = `<div style="margin-top:0.75rem">
    <h3 style="font-size:1rem;margin-bottom:0.5rem">QC Score: ${qc.quality_score}/10</h3>
    <div class="qc-grid">
      <div class="${qc.floating_products?'qc-fail':'qc-pass'}">Floating: ${qc.floating_products?'FAIL':'PASS'}</div>
      <div class="${qc.garbled_text?'qc-fail':'qc-pass'}">Text: ${qc.garbled_text?'FAIL':'PASS'}</div>
      <div class="${qc.detached_shadows?'qc-fail':'qc-pass'}">Shadows: ${qc.detached_shadows?'FAIL':'PASS'}</div>
      <div class="${qc.fake_products?'qc-fail':'qc-pass'}">Fake Products: ${qc.fake_products?'FAIL':'PASS'}</div>
      <div class="${qc.readable_labels?'qc-fail':'qc-pass'}">Labels: ${qc.readable_labels?'PASS':'FAIL'}</div>
    </div>
    ${qc.issues.length ? '<ul style="margin-top:0.5rem;font-size:0.8125rem">'+qc.issues.map(i=>`<li>${i}</li>`).join('')+'</ul>' : ''}
  </div>`;
}

async function refreshSessions() {
  try {
    const data = await fetch('/api/sessions').then(r => r.json());
    const container = $('#sessions-list');
    if (!data.sessions.length) { container.innerHTML = '<div style="text-align:center;color:var(--muted);padding:2rem">No sessions yet</div>'; }
    else {
      container.innerHTML = data.sessions.map(s =>
        `<div class="session-row" data-id="${s.id}">
          <div><strong>${s.id}</strong><br><small>${s.entries.length} entries · ${s.created_at}</small></div>
          <span style="color:var(--muted)">$${s.cost.toFixed(2)}</span>
        </div>`
      ).join('');
      $$('.session-row').forEach(row => row.onclick = () => loadSession(row.dataset.id));
    }
    fetch('/api/costs').then(r => r.json()).then(c => {
      $('#cost-display').innerHTML = `
        <div style="font-size:2rem;color:var(--accent)">$${c.total.toFixed(2)}</div>
        <div style="font-size:0.8125rem;color:var(--muted)">${c.image_count} images across ${c.session_count} sessions</div>
        <div class="cost-bar" style="justify-content:center">
          ${Object.entries(c.by_model).map(([k,v])=>`<div><strong>${k}</strong>: $${v.toFixed(2)}</div>`).join('')}
        </div>`;
    });
  } catch(e) { console.error('sessions', e); }
}
async function loadSession(id) {
  try {
    const data = await fetch(`/api/session/${id}`).then(r => r.json());
    sessionId = id;
    $('#session-timeline').style.display = 'block';
    $('#timeline-content').innerHTML = data.entries.map(e =>
      `<div class="timeline-item">
        ${e.image_url ? `<img src="${e.image_url}">` : ''}
        <div>
          <div><strong>${e.type}</strong> <span style="color:var(--accent)">$${e.cost.toFixed(3)}</span></div>
          <small>${e.created_at}</small>
          <div style="font-size:0.75rem;color:var(--muted);margin-top:0.25rem">${e.note}</div>
        </div>
      </div>`
    ).join('');
  } catch(e) { console.error(e); }
}

refreshSessions();
</script>
</body>
</html>
"""

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
    tier = data.get("tier", "quality")
    session_id = data.get("session_id", new_session_id())
    if not image_path or not changes:
        return jsonify({"error": "image_path and changes required"}), 400

    images = run_cli_refine(image_path, changes, tier)
    for img in images:
        add_entry(session_id, {
            "type": "refine", "cost": img.get("cost", 0), "image_url": img.get("url", ""),
            "model": img.get("model", ""), "note": changes[:100]
        })

    return jsonify({"message": "Refined", "images": images, "session_id": session_id})


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
