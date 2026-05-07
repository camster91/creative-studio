#!/usr/bin/env python3
"""
Creative Studio Web App v4.5
Flask backend with session management, cost tracking, generation, composite, export, QC.
Serves built-in frontend template.
"""

import os
import sys
import json
import time
import uuid
import re
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict

from flask import Flask, render_template_string, request, jsonify, send_from_directory

from figma_utils import parse_figma_url, fetch_figma_context, enhance_prompt_with_figma

# ─── Config ────────────────────────────────────────────────────────────
API_KEY = os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    raise RuntimeError("GEMINI_API_KEY environment variable is required")

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
    return (
        json.loads(path.read_text())
        if path.exists()
        else (default if default is not None else {})
    )


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
    return load_json(
        COST_DB,
        {
            "total": 0.0,
            "by_model": {},
            "by_date": {},
            "session_count": 0,
            "image_count": 0,
        },
    )


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
    return load_json(
        session_path(session_id),
        {"id": session_id, "created_at": now_str(), "entries": []},
    )


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
        + "; ".join(lines)
        + ". Apply each change only to its specified region. Preserve all other areas exactly as they are."
    )


# ─── Image generation wrappers ────────────────────────────────────────

SCRIPT_PATH = str(Path(__file__).parent / "creative_studio.py")


def run_cli_generate(
    prompt: str,
    mode: str,
    tier: str,
    aspect: str,
    smart: bool,
    input_image: Optional[str] = None,
    variations: int = 4,
) -> List[Dict]:
    """Generate images by calling creative_studio.py directly (no bash/uv wrapper)."""
    today = datetime.now().strftime("%Y-%m-%d")
    out_dir = OUTPUT_DIR / today / mode
    out_dir.mkdir(parents=True, exist_ok=True)

    args = [
        sys.executable,
        SCRIPT_PATH,
        "direct",
        "--prompt",
        prompt,
        "--tier",
        tier,
        "--aspect-ratio",
        aspect,
    ]
    if smart:
        args.append("--smart")
    # --format is just output folder name, not needed for generation quality
    # Remove --format to match CLI behavior exactly
    if input_image:
        args += ["--input-image", input_image]

    env = os.environ.copy()
    env["GEMINI_API_KEY"] = API_KEY
    env["CREATIVE_OUTPUT_DIR"] = str(OUTPUT_DIR)

    try:
        subprocess.run(
            args, capture_output=True, text=True, timeout=300, env=env, check=True
        )
        # Find recently generated files
        today_dir = OUTPUT_DIR / today
        files = sorted(
            today_dir.rglob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        now = time.time()
        recent = [f for f in files if (now - f.stat().st_mtime) < 180]
        images = []
        for f in recent[:variations]:
            model_used = (
                "gemini-3.1-flash-image-preview"
                if tier in ("fast", "balanced")
                else "gemini-3-pro-image-preview"
            )
            cost = track_cost(model_used)
            images.append(
                {
                    "path": str(f),
                    "url": image_url(str(f)),
                    "name": f.name,
                    "cost": cost,
                    "model": model_used,
                }
            )
        return images
    except subprocess.CalledProcessError as e:
        return [{"error": f"Generation failed: {e.stderr[:500] if e.stderr else e}"}]
    except Exception as e:
        return [{"error": str(e)}]


def run_cli_composite(
    prompt: str, product_path: str, aspect: str, tier: str = "quality"
) -> List[Dict]:
    out_dir = OUTPUT_DIR / datetime.now().strftime("%Y-%m-%d") / "composite"
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"composite-{int(time.time())}.png"
    out_path = out_dir / fname

    args = [
        sys.executable,
        SCRIPT_PATH,
        "composite",
        "--prompt",
        prompt,
        "--product",
        product_path,
        "--aspect-ratio",
        aspect,
        "--tier",
        tier,
        "--filename",
        fname,
    ]
    env = os.environ.copy()
    env["GEMINI_API_KEY"] = API_KEY

    try:
        subprocess.run(
            args, capture_output=True, text=True, timeout=300, env=env, check=False
        )
        if out_path.exists():
            model_used = "gemini-3-pro-image-preview"
            cost = track_cost(model_used)
            return [
                {
                    "path": str(out_path),
                    "url": image_url(str(out_path)),
                    "name": fname,
                    "cost": cost,
                    "model": model_used,
                }
            ]
    except Exception:
        pass
    return []


def run_cli_export(source_path: str, presets: str) -> List[Dict]:
    args = [
        "bash",
        str(Path(__file__).parent.parent / "launch.sh"),
        "export",
        "--input",
        source_path,
        "--presets",
        presets,
    ]
    env = os.environ.copy()
    env["GEMINI_API_KEY"] = API_KEY
    env["CREATIVE_OUTPUT_DIR"] = str(OUTPUT_DIR)
    try:
        subprocess.run(
            args, capture_output=True, text=True, timeout=120, env=env, check=False
        )
        out_dir = OUTPUT_DIR / datetime.now().strftime("%Y-%m-%d") / "exports"
        files = list(out_dir.glob("*.png"))
        images = []
        for f in files:
            images.append(
                {
                    "path": str(f),
                    "url": image_url(str(f)),
                    "name": f.name,
                    "cost": 0.0,
                    "model": "PIL",
                }
            )
        return images
    except Exception:
        return []


def run_cli_qc(image_path: str) -> dict:
    args = [
        "bash",
        str(Path(__file__).parent.parent / "launch.sh"),
        "qc",
        "--input",
        image_path,
    ]
    env = os.environ.copy()
    env["GEMINI_API_KEY"] = API_KEY
    # Parse stdout for QC results
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=120, env=env, check=False
        )
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
    args = [
        "bash",
        str(Path(__file__).parent.parent / "launch.sh"),
        "direct",
        "--prompt",
        f"Based on this reference image, make these changes: {changes}",
        "--input-image",
        image_path,
        "--tier",
        tier,
        "--filename",
        fname,
    ]
    env = os.environ.copy()
    env["GEMINI_API_KEY"] = API_KEY
    try:
        subprocess.run(
            args, capture_output=True, text=True, timeout=300, env=env, check=False
        )
        out_path = out_dir / fname
        if not out_path.exists():
            # Fallback: search for any newly created png in output dir
            today_dir = OUTPUT_DIR / datetime.now().strftime("%Y-%m-%d")
            files = sorted(
                today_dir.rglob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True
            )
            if files:
                out_path = files[0]
        if out_path.exists():
            model_used = (
                "gemini-3.1-flash-image-preview"
                if tier in ("fast", "balanced")
                else "gemini-3-pro-image-preview"
            )
            cost = track_cost(model_used)
            return [
                {
                    "path": str(out_path),
                    "url": image_url(str(out_path)),
                    "name": out_path.name,
                    "cost": cost,
                    "model": model_used,
                }
            ]
    except Exception:
        pass
    return []


# ─── Flask App ─────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32MB uploads

# ── Frontend HTML ─────────────────────────────────────────────────────

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Creative Studio — AI Image Generator</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #0f1117;
  --bg-2: #181b24;
  --surface: rgba(255,255,255,0.04);
  --surface-hover: rgba(255,255,255,0.08);
  --border: rgba(255,255,255,0.10);
  --border-strong: rgba(255,255,255,0.18);
  --text: #f1f5f9;
  --text-secondary: #a1aab8;
  --text-dim: #7a8494;
  --primary: #FF7A59;
  --primary-dim: rgba(255,122,89,0.18);
  --accent: #FFD166;
  --ok: #2dd4a8;
  --bad: #f87171;
  --font: 'Inter', system-ui, sans-serif;
  --radius: 12px;
  --radius-sm: 8px;
}

* { box-sizing:border-box; margin:0; padding:0 }
html, body { height:100%; font-size:16px; }
body {
  font-family: var(--font);
  background: var(--bg);
  color: var(--text);
  overflow-x: hidden;
  line-height: 1.55;
}

.app { display:flex; flex-direction:column; height:100vh; }
.header {
  display:flex; align-items:center; justify-content:space-between;
  padding: 0 28px; height: 56px;
  border-bottom: 1px solid var(--border); flex-shrink:0;
}
.header-left { display:flex; align-items:center; gap:12px; }
.header-brand { font-weight:700; font-size:1.1rem; letter-spacing:-0.01em; }
.header-brand span { color: var(--text-secondary); font-weight:500; }
.header-right { display:flex; align-items:center; gap:14px; }

.main { flex:1; display:flex; overflow:hidden; }

.sidebar {
  width: 320px; min-width: 280px;
  border-right: 1px solid var(--border);
  display:flex; flex-direction:column;
  overflow-y:auto; scrollbar-width: thin;
}
.sidebar-body { padding: 22px 22px 28px; display:flex; flex-direction:column; gap:18px; }

.history-sidebar {
  width: 260px; min-width: 200px;
  border-left: 1px solid var(--border);
  display:flex; flex-direction:column;
  background: var(--bg-2);
  overflow-y:auto; scrollbar-width: thin;
}
.history-header {
  padding: 16px; font-size:0.75rem; font-weight:700; color:var(--text-secondary);
  text-transform:uppercase; border-bottom: 1px solid var(--border);
  display:flex; justify-content:space-between; align-items:center;
}
.history-list { padding: 12px; display:flex; flex-direction:column; gap:10px; }
.history-item {
  border-radius: var(--radius-sm); border: 1px solid var(--border);
  overflow:hidden; cursor:pointer; background: var(--bg);
  transition: transform 0.15s;
}
.history-item:hover { border-color: var(--primary); transform: scale(1.02); }
.history-item img { width:100%; aspect-ratio:1; object-fit:cover; display:block; }
.history-item .info { padding: 6px 10px; font-size: 0.65rem; color: var(--text-dim); }

.label {
  display:block; font-size:0.82rem; font-weight:600;
  color: var(--text-secondary); margin-bottom:8px;
}
.btn-toggle {
  font-size:0.75rem; font-weight:600; color: var(--primary);
  background:transparent; border:none; cursor:pointer; padding:2px 8px;
  border-radius: 6px; transition: background 0.15s;
}
.btn-toggle:hover { background: var(--primary-dim); }

input[type="text"], input[type="url"], textarea, select {
  width:100%; padding: 11px 14px;
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  background: var(--surface); color: var(--text);
  font-family: var(--font); font-size:0.95rem;
  transition: border-color 0.2s, box-shadow 0.2s;
}
textarea { min-height: 90px; resize:vertical; }
input:focus, textarea:focus, select:focus {
  outline:none; border-color: var(--primary);
  box-shadow: 0 0 0 3px rgba(255,122,89,0.12);
}
input::placeholder, textarea::placeholder { color: var(--text-dim); opacity:0.9; }

.section-box {
  border: 1px solid var(--border); border-radius: var(--radius);
  background: var(--bg-2); padding: 18px;
}
.section-title {
  font-size:0.88rem; font-weight:700; color: var(--text-secondary);
  text-transform:uppercase; letter-spacing:0.03em; margin-bottom:14px;
}

.dropzone {
  border: 2px dashed var(--border-strong); border-radius: var(--radius);
  padding: 24px 16px; text-align:center; cursor:pointer;
  transition: border-color 0.2s, background 0.2s;
  background: var(--surface);
}
.dropzone:hover, .dropzone.drag { border-color: var(--primary); background: var(--primary-dim); }
.dropzone .icon { font-size:1.8rem; margin-bottom:8px; opacity:0.7; }
.dropzone p { color: var(--text-secondary); font-size:0.88rem; margin-bottom:4px; }
.dropzone .hint { color: var(--text-dim); font-size:0.75rem; }
.dropzone input { display:none; }
.preview-thumb { width:100%; max-height:160px; object-fit:contain; border-radius:var(--radius-sm); margin-top:10px; }

.tier-row { display:flex; gap:8px; }
.tier-btn {
  flex:1; padding: 10px 8px; border-radius: var(--radius-sm);
  border: 1px solid var(--border); background: var(--surface);
  color: var(--text-secondary); font-size:0.82rem; font-weight:600;
  cursor:pointer; transition: all 0.15s; text-align:center;
}
.tier-btn:hover { border-color: var(--border-strong); background: var(--surface-hover); }
.tier-btn.selected { border-color: var(--primary); background: var(--primary-dim); color:var(--primary); }
.tier-btn .sub { display:block; font-size:0.7rem; font-weight:500; margin-top:3px; color:var(--text-dim); }

.aspect-row { display:flex; gap:6px; flex-wrap:wrap; }
.aspect-chip {
  padding: 7px 14px; border-radius: 20px; border: 1px solid var(--border);
  background: var(--surface); color: var(--text-secondary);
  font-size:0.8rem; font-weight:600; cursor:pointer; transition: all 0.15s;
}
.aspect-chip:hover { background: var(--surface-hover); }
.aspect-chip.selected { border-color: var(--primary); background: var(--primary-dim); color:var(--primary); }

.btn-generate {
  width:100%; padding: 14px; border-radius: var(--radius-sm); border:none;
  background: var(--primary); color: #fff; font-size:1rem; font-weight:700;
  cursor:pointer; transition: all 0.15s; letter-spacing:0.01em;
}
.btn-generate:hover { background: #ff6340; transform: translateY(-1px); }
.btn-generate:disabled { opacity:0.5; cursor:not-allowed; transform:none; }

.canvas-area {
  flex:1; display:flex; flex-direction:column; align-items:center; justify-content:center;
  padding: 28px; position:relative; overflow:auto;
}
.canvas-inner { width:100%; display:flex; flex-direction:column; align-items:center; gap:20px; }
.canvas-image-wrap {
  position:relative; display:inline-block;
  border-radius: var(--radius); overflow:hidden;
  border: 1px solid var(--border);
  box-shadow: 0 12px 40px rgba(0,0,0,0.5);
}
.canvas-image-wrap img { max-width: 100%; max-height: min(60vh, 600px); display:block; cursor: crosshair; }
.pin {
  position: absolute; width: 24px; height: 24px;
  background: var(--primary); color: white;
  border-radius: 50%; display: flex; align-items: center; justify-content: center;
  font-size: 12px; font-weight: 700; cursor: pointer;
  box-shadow: 0 0 0 3px rgba(255,122,89,0.3);
  transform: translate(-50%, -50%); z-index: 10;
}
.pin:hover { transform: translate(-50%, -50%) scale(1.1); background: #ff6340; }
.pin-tooltip {
  position: absolute; bottom: 32px; left: 50%; transform: translateX(-50%);
  background: #000; color: white; padding: 4px 8px; border-radius: 4px;
  font-size: 0.7rem; white-space: nowrap; pointer-events: none; opacity: 0;
  transition: opacity 0.2s;
}
.pin:hover .pin-tooltip { opacity: 1; }

.output-grid {
  display:grid; grid-template-columns: repeat(auto-fill, minmax(160px,1fr)); gap:12px;
  width:100%; max-width: 900px;
}
.output-cell {
  border-radius: var(--radius-sm); overflow:hidden;
  border: 1px solid var(--border); cursor:pointer;
  transition: transform 0.15s, box-shadow 0.15s;
  position:relative;
}
.output-cell:hover { transform: translateY(-2px); box-shadow: 0 8px 24px rgba(0,0,0,0.4); }
.output-cell img { width:100%; aspect-ratio:1; object-fit:cover; display:block; }
.output-tag {
  position:absolute; bottom:6px; right:6px;
  background:rgba(0,0,0,0.65); backdrop-filter:blur(4px);
  padding:3px 8px; border-radius:6px; font-size:0.65rem; font-weight:600;
  color:#fff;
}

.empty { text-align:center; color: var(--text-dim); padding: 40px 20px; }
.empty .icon { font-size:3.5rem; margin-bottom:16px; opacity:0.3; }
.empty h2 { color: var(--text-secondary); font-size:1.1rem; margin-bottom:8px; }
.empty p { font-size:0.9rem; max-width:320px; margin:0 auto; }

.spinner-wrap { display:flex; flex-direction:column; align-items:center; gap:14px; }
.spinner {
  width: 36px; height: 36px; border: 3px solid var(--border);
  border-top-color: var(--primary); border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
.spinner-text { color: var(--text-secondary); font-size:0.9rem; }
@keyframes spin { to { transform: rotate(360deg); } }

.advanced { display:none; }
.advanced.open { display:flex; flex-direction:column; gap:18px; }

.image-actions {
  display:flex; gap:10px; margin-top:14px; width:100%; justify-content:center;
}
.btn-action {
  padding: 8px 16px; border-radius: var(--radius-sm);
  border: 1px solid var(--border-strong); background: var(--bg-2);
  color: var(--text-secondary); font-size:0.82rem; font-weight:600;
  cursor:pointer; transition: all 0.15s;
}
.btn-action:hover { border-color: var(--primary); color: var(--text); background: var(--surface-hover); }

.dropdown { position: relative; display: inline-block; }
.dropdown-content {
  display: none; position: absolute; bottom: 100%; left: 0;
  background-color: var(--bg-2); min-width: 220px;
  box-shadow: 0 8px 16px rgba(0,0,0,0.5);
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  z-index: 1; margin-bottom: 8px; padding: 12px;
}
.dropdown-content label {
  display: flex; align-items: center; gap: 8px;
  padding: 7px 4px; cursor: pointer;
  color: var(--text-secondary); font-size: 0.82rem;
  border-radius: 4px; transition: background 0.15s;
}
.dropdown-content label:hover { background: var(--surface-hover); color: var(--text); }
.dropdown-content input[type="checkbox"] {
  width: 15px; height: 15px; accent-color: var(--primary); flex-shrink: 0;
}
.dropdown-export-btn {
  width: 100%; margin-top: 10px; padding: 9px;
  border-radius: var(--radius-sm); border: none;
  background: var(--primary); color: #fff;
  font-size: 0.82rem; font-weight: 700; cursor: pointer;
  transition: background 0.15s;
}
.dropdown-export-btn:hover { background: #ff6340; }
.dropdown-export-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.dropdown:hover .dropdown-content { display: block; }

.export-results {
  width: 100%; max-width: 900px; margin-top: 16px;
  display: flex; flex-direction: column; gap: 12px;
}
.export-results-title {
  font-size: 0.88rem; font-weight: 700; color: var(--text-secondary);
  text-transform: uppercase; letter-spacing: 0.03em;
}
.export-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 10px;
}
.export-card {
  border-radius: var(--radius-sm); overflow: hidden;
  border: 1px solid var(--border); background: var(--bg-2);
  transition: transform 0.15s, box-shadow 0.15s;
}
.export-card:hover { transform: translateY(-2px); box-shadow: 0 8px 24px rgba(0,0,0,0.4); }
.export-card img { width: 100%; aspect-ratio: 1; object-fit: cover; display: block; }
.export-card-body { padding: 8px 10px; }
.export-card-label { font-size: 0.72rem; font-weight: 700; color: var(--text-secondary); text-transform: uppercase; }
.export-card-dims { font-size: 0.68rem; color: var(--text-dim); }
.export-card-dl {
  display: block; width: 100%; margin-top: 6px; padding: 6px;
  border-radius: 4px; border: 1px solid var(--border-strong);
  background: var(--surface); color: var(--text-secondary);
  font-size: 0.72rem; font-weight: 600; cursor: pointer; text-align: center;
  text-decoration: none; transition: all 0.15s;
}
.export-card-dl:hover { border-color: var(--primary); color: var(--text); background: var(--surface-hover); }

.preset-tag {
  display: inline-block; padding: 2px 8px; border-radius: 4px;
  background: var(--primary-dim); color: var(--primary);
  font-size: 0.65rem; font-weight: 700; margin: 2px;
  text-transform: uppercase; letter-spacing: 0.03em;
}
.qc-panel {
  width: 100%; max-width: 900px; margin-top: 16px; padding: 18px;
  background: var(--bg-2); border: 1px solid var(--border);
  border-radius: var(--radius); text-align: left;
}
.qc-header { display:flex; align-items:flex-start; gap:20px; margin-bottom:14px; }
.qc-ring-wrap { flex-shrink:0; position:relative; width:72px; height:72px; }
.qc-ring-wrap svg { width:72px; height:72px; transform: rotate(-90deg); }
.qc-ring-bg { fill:none; stroke:var(--border-strong); stroke-width:6; }
.qc-ring-fill { fill:none; stroke-width:6; stroke-linecap:round;
  transition: stroke-dashoffset 0.6s ease, stroke 0.3s ease; }
.qc-ring-label {
  position:absolute; inset:0; display:flex; align-items:center;
  justify-content:center; font-size:1.05rem; font-weight:700;
}
.qc-title { flex:1; }
.qc-title h3 { font-size:0.95rem; font-weight:700; margin-bottom:4px; }
.qc-title .qc-subtitle { font-size:0.75rem; color:var(--text-dim); }
.qc-badge-row { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:12px; }
.qc-badge {
  display:inline-flex; align-items:center; gap:6px;
  padding: 5px 12px; border-radius:20px;
  font-size:0.75rem; font-weight:700; letter-spacing:0.02em;
}
.qc-badge.pass { background:rgba(45,212,168,0.15); border:1px solid rgba(45,212,168,0.35); color:var(--ok); }
.qc-badge.fail { background:rgba(248,113,113,0.15); border:1px solid rgba(248,113,113,0.35); color:var(--bad); }
.qc-badge.neutral { background:var(--surface); border:1px solid var(--border-strong); color:var(--text-secondary); }
.qc-criteria { display:grid; grid-template-columns:repeat(auto-fit, minmax(180px,1fr)); gap:8px; margin-bottom:14px; }
.qc-criterion { display:flex; align-items:center; gap:8px; padding:8px 12px; border-radius:var(--radius-sm); background:var(--surface); font-size:0.8rem; }
.qc-criterion .qc-icon { font-size:0.95rem; flex-shrink:0; }
.qc-criterion .qc-label { flex:1; color:var(--text-secondary); }
.qc-criterion .qc-status { font-weight:700; font-size:0.75rem; }
.qc-criterion.pass { border-left:3px solid var(--ok); }
.qc-criterion.pass .qc-status { color:var(--ok); }
.qc-criterion.fail { border-left:3px solid var(--bad); }
.qc-criterion.fail .qc-status { color:var(--bad); }
.qc-issues { margin-top:10px; }
.qc-issues-title { font-size:0.75rem; font-weight:700; color:var(--text-dim); margin-bottom:6px; text-transform:uppercase; letter-spacing:0.05em; }
.qc-issue { font-size:0.78rem; color:var(--text-secondary); padding:4px 0; border-bottom:1px solid var(--border); }
.qc-issue:last-child { border-bottom:none; }
.qc-footer { margin-top:14px; padding-top:12px; border-top:1px solid var(--border); display:flex; align-items:center; justify-content:space-between; }
.qc-footer a { color:var(--primary); text-decoration:none; font-size:0.78rem; font-weight:600; }
.qc-footer a:hover { text-decoration:underline; }
.qc-footer span { font-size:0.72rem; color:var(--text-dim); }
.qc-pass-banner { display:inline-flex; align-items:center; gap:6px; padding:4px 14px; border-radius:20px; background:rgba(45,212,168,0.15); border:1px solid rgba(45,212,168,0.3); color:var(--ok); font-size:0.8rem; font-weight:700; margin-bottom:10px; }
.qc-fail-banner { display:inline-flex; align-items:center; gap:6px; padding:4px 14px; border-radius:20px; background:rgba(248,113,113,0.15); border:1px solid rgba(248,113,113,0.3); color:var(--bad); font-size:0.8rem; font-weight:700; margin-bottom:10px; }

.qc-panel {
  display:flex; align-items:center; gap:12px; padding: 10px 22px;
  border-top: 1px solid var(--border); font-size:0.82rem;
  color: var(--text-dim); background: var(--bg-2);
}
.status-bar .cost { color: var(--accent); font-weight:700; margin-left:auto; }

@media (max-width: 900px) {
  .main { flex-direction:column; }
  .sidebar { width:100%; max-width:none; border-right:none; border-bottom:1px solid var(--border); max-height:45vh; }
  .canvas-area { padding: 16px; }
  .output-grid { grid-template-columns: repeat(2, 1fr); }
}
@media (max-width: 500px) {
  html { font-size:14px; }
  .header { padding: 0 14px; }
  .header-brand span { display:none; }
  .sidebar-body { padding: 14px; }
  .canvas-image-wrap img { max-height: 45vh; }
  .output-grid { grid-template-columns: 1fr; }
}

.toast {
  position:fixed; bottom:24px; left:50%; transform:translateX(-50%);
  padding: 10px 22px; border-radius: 30px; font-size:0.88rem; font-weight:600;
  z-index:200; display:none; box-shadow: 0 8px 32px rgba(0,0,0,0.5);
}
.toast.show { display:block; animation: toastIn 0.3s ease; }
.toast.ok { background: rgba(45,212,168,0.15); border:1px solid rgba(45,212,168,0.3); color: var(--ok); }
.toast.err { background: rgba(248,113,113,0.15); border:1px solid rgba(248,113,113,0.3); color: var(--bad); }
@keyframes toastIn { from { opacity:0; transform:translateX(-50%) translateY(12px); } to { opacity:1; transform:translateX(-50%) translateY(0); } }
</style>
</head>
<body>
<div class="app">
  <div class="header">
    <div class="header-left">
      <span class="header-brand">🎨 Creative <span>Studio</span></span>
    </div>
    <div class="header-right">
      <button class="btn-toggle" id="advancedToggle" onclick="toggleAdvanced()">⚙️ Advanced</button>
    </div>
  </div>

  <div class="main">
    <div class="sidebar">
      <div class="sidebar-body">
        <div>
          <div class="label">📎 Reference Image (optional)</div>
          <div class="dropzone" id="dropzone" onclick="document.getElementById('refFile').click()"
               ondragover="event.preventDefault();this.classList.add('drag')"
               ondragleave="this.classList.remove('drag')"
               ondrop="handleDrop(event)">
            <div class="icon">🖼️</div>
            <p>Click or drop your product photo</p>
            <span class="hint">Helps the AI keep your exact product</span>
            <img id="thumb" class="preview-thumb" style="display:none;">
            <input type="file" id="refFile" accept="image/*" onchange="handleFile(this.files[0])">
          </div>
        </div>

        <div>
          <div class="label">✏️ Prompt</div>
          <textarea id="prompt" placeholder="e.g. A premium supplement tub on a clean wooden shelf in a GNC store, warm overhead lighting, product photography"></textarea>
        </div>

        <div>
          <div class="label">🏎️ Quality / Speed</div>
          <div class="tier-row" id="tierRow">
            <button class="tier-btn selected" data-tier="fast" onclick="setTier(this,'fast')">Fast<span class="sub">~$0.07 · draft</span></button>
            <button class="tier-btn" data-tier="balanced" onclick="setTier(this,'balanced')">Balanced<span class="sub">~$0.07 · 2K</span></button>
            <button class="tier-btn" data-tier="quality" onclick="setTier(this,'quality')">Quality<span class="sub">~$0.20 · 2K</span></button>
          </div>
        </div>

        <div>
          <label class="label-row" style="cursor:pointer; display:flex; justify-content:space-between; align-items:center; background: var(--surface); padding: 12px; border-radius: var(--radius-sm); border: 1px solid var(--border);">
            <div style="display:flex; flex-direction:column">
              <span style="font-size:0.88rem; font-weight:700">🧊 Composite Mode</span>
              <span style="font-size:0.7rem; color:var(--text-dim)">Real product + AI scene</span>
            </div>
            <input type="checkbox" id="compositeCheck" onchange="toggleComposite(this.checked)" style="width:18px; height:18px; accent-color:var(--primary)">
          </label>
        </div>

        <div id="productUpload" style="display:none">
          <div class="label">🛍️ Product Photo (cutout)</div>
          <div class="dropzone" id="dropzoneProd" onclick="$('prodFile').click()"
               ondragover="event.preventDefault();this.classList.add('drag')"
               ondragleave="this.classList.remove('drag')"
               ondrop="handleDropProd(event)">
            <div class="icon">🏷️</div>
            <p>Upload your real product</p>
            <img id="thumbProd" class="preview-thumb" style="display:none;">
            <input type="file" id="prodFile" accept="image/*" onchange="handleFileProd(this.files[0])">
          </div>
        </div>

        <button class="btn-generate" id="genBtn" onclick="generate()">⚡ Generate Image</button>

        <div class="advanced" id="advancedPanel">
          <div class="section-box">
            <div class="section-title">Settings</div>
            <div style="margin-bottom:14px">
              <div class="label">Aspect Ratio</div>
              <div class="aspect-row" id="aspectRow">
                <button class="aspect-chip selected" data-ratio="1:1" onclick="setRatio(this,'1:1')">1:1</button>
                <button class="aspect-chip" data-ratio="16:9" onclick="setRatio(this,'16:9')">16:9</button>
                <button class="aspect-chip" data-ratio="4:3" onclick="setRatio(this,'4:3')">4:3</button>
                <button class="aspect-chip" data-ratio="9:16" onclick="setRatio(this,'9:16')">9:16</button>
                <button class="aspect-chip" data-ratio="3:2" onclick="setRatio(this,'3:2')">3:2</button>
              </div>
            </div>
            <div>
              <label class="label-row" style="cursor:pointer">
                <span>🧠 Smart prompt enhancement</span>
                <input type="checkbox" id="smartCheck">
              </label>
              <p style="font-size:0.78rem; color:var(--text-dim); margin-top:6px">
                Automatically improves lighting, camera angle, and material descriptors.
              </p>
            </div>
            <div style="margin-top:14px">
              <div class="label">🎨 Figma Integration (Brand Context)</div>
              <input type="text" id="figmaUrl" placeholder="https://www.figma.com/design/..." onchange="loadFigmaContext(this.value)">
              <div id="figmaStatus" style="font-size:0.72rem; color:var(--text-dim); margin-top:6px"></div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div class="canvas-area" id="canvas">
      <div class="canvas-inner" id="canvasInner">
        <div class="empty" id="emptyState">
          <div class="icon">🎨</div>
          <h2>Ready to create</h2>
          <p>Upload a reference image, type your scene description, and click Generate.</p>
        </div>
        <div class="spinner-wrap" id="spinner" style="display:none">
          <div class="spinner"></div>
          <p class="spinner-text">Generating image… this takes 10–30 seconds</p>
        </div>
        <div class="canvas-image-wrap" id="imageWrap" style="display:none">
          <img id="mainImg" src="">
          <div class="image-actions">
            <button class="btn-action" onclick="runQC()">🔍 Run QC Audit</button>
            <div class="dropdown">
              <button class="btn-action">📤 Export to... ▼</button>
              <div class="dropdown-content">
                <label><input type="checkbox" value="amazon"> Amazon (1:1 White)</label>
                <label><input type="checkbox" value="shopify"> Shopify (2048×2048)</label>
                <label><input type="checkbox" value="meta-feed"> Meta Feed (4:5)</label>
                <label><input type="checkbox" value="meta-stories"> Meta Stories (9:16)</label>
                <label><input type="checkbox" value="pinterest"> Pinterest (2:3)</label>
                <label><input type="checkbox" value="web-hero"> Web Hero (16:9)</label>
                <label><input type="checkbox" value="print-dpi"> Print (300 DPI)</label>
                <button class="dropdown-export-btn" id="exportBtn" onclick="exportSelected()">Export Selected</button>
              </div>
            </div>
          </div>
          <div id="qcResults" class="qc-panel" style="display:none"></div>
          <div id="refinePanel" class="refine-panel" style="display:none">
            <div class="label">🎯 Active Area Fixes</div>
            <p style="font-size:0.75rem; color:var(--text-dim); margin-bottom:8px">
              Click the image to add more fixes.
            </p>
            <button class="btn-generate btn-refine" onclick="refine()">✨ Refine this image</button>
          </div>
          <div id="exportResults" class="export-results" style="display:none"></div>
        </div>
        <div class="output-grid" id="outputGrid" style="display:none"></div>
      </div>
    </div>

    <div class="history-sidebar">
      <div class="history-header">
        <span>Session History</span>
        <button class="btn-toggle" onclick="newSession()">New</button>
      </div>
      <div class="history-list" id="historyList">
        <!-- History items go here -->
      </div>
    </div>
  </div>

  <div class="status-bar">
    <span>💰 Cost today: <span class="cost" id="todayCost">$0.00</span></span>
    <span id="statusText" style="margin-left:auto"></span>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
let state = { tier:'fast', ratio:'1:1', refImage:null, prodImage:null, generating:false, composite:false, sessionId:null, currentImageUrl:null };
function $(id){ return document.getElementById(id); }
function showToast(msg,type='ok'){
  const t=$('toast'); t.textContent=msg; t.className='toast show '+type;
  setTimeout(()=> t.classList.remove('show'), 3000);
}
function setTier(el,tier){
  state.tier=tier;
  document.querySelectorAll('.tier-btn').forEach(b=> b.classList.remove('selected'));
  el.classList.add('selected');
}
function setRatio(el,ratio){
  state.ratio=ratio;
  document.querySelectorAll('.aspect-chip').forEach(b=> b.classList.remove('selected'));
  el.classList.add('selected');
}
function toggleAdvanced(){
  const p=$('advancedPanel'), btn=$('advancedToggle');
  p.classList.toggle('open');
  btn.textContent = p.classList.contains('open') ? '⚙️ Hide Advanced' : '⚙️ Advanced';
}
function newSession(){
  state.sessionId = null;
  state.currentImageUrl = null;
  $('historyList').innerHTML = '';
  $('imageWrap').style.display = 'none';
  $('outputGrid').style.display = 'none';
  $('emptyState').style.display = 'block';
  showToast('New session started');
}
async function loadHistory(){
  if(!state.sessionId) return;
  try {
    const resp = await fetch('/api/session/' + state.sessionId);
    const data = await resp.json();
    const list = $('historyList');
    list.innerHTML = data.entries.reverse().map(e => `
      <div class="history-item" onclick="pickImage('${e.image_url}')">
        <img src="${e.image_url}">
        <div class="info">
          <div style="font-weight:700; color:var(--text)">${e.type.toUpperCase()}</div>
          <div style="white-space:nowrap; overflow:hidden; text-overflow:ellipsis">${e.prompt || e.note || ''}</div>
        </div>
      </div>
    `).join('');
  } catch(e) { console.error('History failed', e); }
}
function toggleComposite(checked){
  state.composite = checked;
  $('productUpload').style.display = checked ? 'block' : 'none';
  $('genBtn').textContent = checked ? '🪄 Create Composite' : '⚡ Generate Image';
}
async function loadFigmaContext(url){
  if(!url) { $('figmaStatus').textContent=''; return; }
  $('figmaStatus').textContent = '⌛ Fetching brand context...';
  try {
    const resp = await fetch('/api/figma', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url })
    });
    const data = await resp.json();
    if(data.error) throw new Error(data.error);
    const colors = (data.fills||[]).slice(0,3).join(', ');
    const fonts = (data.fonts||[]).slice(0,2).join(', ');
    $('figmaStatus').innerHTML = `<span style="color:var(--ok)">✅ Connected:</span> ${colors} ${fonts}`;
  } catch(e) {
    $('figmaStatus').innerHTML = `<span style="color:var(--bad)">❌ Error:</span> ${e.message}`;
  }
}
function handleFile(file){
  if(!file) return;
  state.refImage = file;
  const reader = new FileReader();
  reader.onload = e => { $('thumb').src=e.target.result; $('thumb').style.display='block'; };
  reader.readAsDataURL(file);
}
function handleDrop(e){
  e.preventDefault(); e.currentTarget.classList.remove('drag');
  const file = e.dataTransfer.files[0];
  if(file) handleFile(file);
}
function handleFileProd(file){
  if(!file) return;
  state.prodImage = file;
  const reader = new FileReader();
  reader.onload = e => { $('thumbProd').src=e.target.result; $('thumbProd').style.display='block'; };
  reader.readAsDataURL(file);
}
function handleDropProd(e){
  e.preventDefault(); e.currentTarget.classList.remove('drag');
  const file = e.dataTransfer.files[0];
  if(file) handleFileProd(file);
}
async function generate(){
  const prompt = $('prompt').value.trim();
  const figma_url = $('figmaUrl').value.trim();
  if(!prompt){ showToast('Please enter a prompt','err'); return; }
  if(state.generating) return;

  if(state.composite && !state.prodImage){
    showToast('Product photo required for Composite Mode','err'); return;
  }

  state.generating=true; $('genBtn').disabled=true;
  $('emptyState').style.display='none';
  $('spinner').style.display='flex';
  $('imageWrap').style.display='none';
  $('outputGrid').style.display='none';

  try {
    let resp;
    if(state.composite) {
      const fd = new FormData();
      fd.append('prompt', prompt);
      fd.append('product', state.prodImage);
      fd.append('aspect_ratio', state.ratio);
      fd.append('tier', state.tier);
      if(state.sessionId) fd.append('session_id', state.sessionId);
      if(figma_url) fd.append('figma_url', figma_url);
      resp = await fetch('/api/composite', { method:'POST', body:fd });
    } else {
      const body = { prompt: prompt, mode:'direct', tier:state.tier,
        aspect_ratio:state.ratio, smart:$('smartCheck')?.checked??false, variations:4 };
      if(state.sessionId) body.session_id = state.sessionId;
      if(figma_url) body.figma_url = figma_url;
      resp = await fetch('/api/generate',{
        method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)
      });
    }

    const data = await resp.json();
    $('spinner').style.display='none';
    if(data.error || (data.images&&data.images[0]?.error)){
      showToast(data.error||data.images[0].error||'Generation failed','err');
      $('emptyState').style.display='block'; return;
    }
    if(data.images&&data.images.length){
      state.sessionId = data.session_id;
      renderImages(data.images);
      showToast(data.message||'Done!','ok');
      refreshCost();
      loadHistory();
    } else { showToast('No images returned','err'); $('emptyState').style.display='block'; }
  }catch(e){
    $('spinner').style.display='none'; $('emptyState').style.display='block';
    showToast('Network error: '+e.message,'err');
  }finally{ state.generating=false; $('genBtn').disabled=false; }
}
function renderImages(images){
  const wrap = $('imageWrap'), grid = $('outputGrid');
  if(images.length===1){
    wrap.style.display='inline-block'; grid.style.display='none';
    $('mainImg').src = images[0].url;
    state.currentImageUrl = images[0].url;
    loadPins(images[0].url);
  } else {
    wrap.style.display='none'; grid.style.display='grid';
    grid.innerHTML = images.map(img=>
      `<div class="output-cell" onclick="pickImage('${img.url}')">
        <img src="${img.url}" loading="lazy">
        <div class="output-tag">${img.model.replace('gemini-3.1-','').replace('gemini-3-','')}</div>
      </div>`).join('');
  }
}
function pickImage(url){
  const wrap=$('imageWrap'), grid=$('outputGrid');
  wrap.style.display='inline-block'; grid.style.display='none';
  $('mainImg').src=url;
  state.currentImageUrl = url;
  $('qcResults').style.display = 'none';
  loadPins(url);
}

// -- Pin Management ---------------------------------------------------
let currentPins = [];

$('mainImg').onclick = function(e) {
  if (state.generating) return;
  const rect = e.target.getBoundingClientRect();
  const x = (e.clientX - rect.left) / rect.width;
  const y = (e.clientY - rect.top) / rect.height;
  const text = prompt("What should we change in this area?");
  if (text) addPin(x, y, text);
};

async function addPin(x, y, text) {
  try {
    const resp = await fetch('/api/pins', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ image_path: state.currentImageUrl, x, y, text })
    });
    const data = await resp.json();
    renderPins(data.pins);
  } catch(e) { showToast('Failed to add pin', 'err'); }
}

async function loadPins(url) {
  try {
    const resp = await fetch('/api/pins/' + encodeURIComponent(url));
    const data = await resp.json();
    renderPins(data.pins);
  } catch(e) { console.log('Load pins failed', e); }
}

function renderPins(pins) {
  currentPins = pins;
  // Remove existing pins from DOM
  document.querySelectorAll('.pin').forEach(p => p.remove());
  const wrap = $('imageWrap');
  pins.forEach((p, i) => {
    const el = document.createElement('div');
    el.className = 'pin';
    el.style.left = (p.x * 100) + '%';
    el.style.top = (p.y * 100) + '%';
    el.innerHTML = `${i+1}<div class="pin-tooltip">${p.text}</div>`;
    el.onclick = (e) => { e.stopPropagation(); if(confirm('Delete this pin?')) deletePin(p.id); };
    wrap.appendChild(el);
  });
  updateRefinePanel();
}

async function deletePin(pinId) {
  try {
    const resp = await fetch(`/api/pins/${encodeURIComponent(state.currentImageUrl)}/${pinId}`, { method: 'DELETE' });
    const data = await resp.json();
    renderPins(data.pins);
  } catch(e) { showToast('Delete failed', 'err'); }
}

function updateRefinePanel() {
  const panel = $('refinePanel');
  if (currentPins.length > 0) {
    panel.style.display = 'flex';
  } else {
    panel.style.display = 'none';
  }
}

async function refine() {
  const changes = prompt("Any overall changes? (optional)");
  const figma_url = $('figmaUrl').value.trim();
  if (state.generating) return;
  state.generating = true;
  showToast('Refining image...', 'ok');
  try {
    const resp = await fetch('/api/refine', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        image_path: state.currentImageUrl,
        changes: changes || "",
        pins: currentPins,
        tier: state.tier,
        session_id: state.sessionId,
        figma_url: figma_url
      })
    });
    const data = await resp.json();
    if(data.error) throw new Error(data.error);
    renderImages(data.images);
    showToast('Refined!');
    loadHistory();
  } catch(e) {
    showToast('Refinement failed: ' + e.message, 'err');
  } finally { state.generating = false; }
}

async function runQC(){
  const imgUrl = $('mainImg').getAttribute('src');
  if(!imgUrl) return;
  showToast('Running vision audit...','ok');
  $('qcResults').style.display = 'none';
  try {
    const resp = await fetch('/api/qc', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ image_url: imgUrl })
    });
    const data = await resp.json();
    if(data.error) throw new Error(data.error);

    const q = data.qc;
    const items = [
      { key: 'Floating Products', val: q.floating_products },
      { key: 'Garbled Text', val: q.garbled_text },
      { key: 'Detached Shadows', val: q.detached_shadows },
      { key: 'Fake Products', val: q.fake_products },
      { key: 'Readable Labels', val: q.readable_labels, invert: true }
    ];

    $('qcResults').innerHTML = `
      ${q.quality_score >= 7
        ? '<div class="qc-pass-banner">✅ QC PASSED — Image looks production-ready</div>'
        : '<div class="qc-fail-banner">⚠️ QC FLAGGED — Review issues before use</div>'}
      <div class="qc-header">
        <div class="qc-ring-wrap">
          <svg viewBox="0 0 36 36">
            <circle class="qc-ring-bg" cx="18" cy="18" r="15.9"/>
            <circle class="qc-ring-fill" cx="18" cy="18" r="15.9"
              stroke="${q.quality_score >= 7 ? 'var(--ok)' : q.quality_score >= 4 ? 'var(--accent)' : 'var(--bad)'}"
              stroke-dasharray="${q.quality_score * 10}, 100"
              stroke-dashoffset="${100 - q.quality_score * 10}"/>
          </svg>
          <div class="qc-ring-label" style="color:${q.quality_score >= 7 ? 'var(--ok)' : q.quality_score >= 4 ? 'var(--accent)' : 'var(--bad)'}">${q.quality_score}</div>
        </div>
        <div class="qc-title">
          <h3>Quality Score ${q.quality_score}/10</h3>
          <div class="qc-subtitle">Vision AI inspection — 5 criteria checked</div>
        </div>
      </div>
      <div class="qc-badge-row">
        ${items.map(it => {
          const isPass = it.invert ? it.val : !it.val;
          return `<span class="qc-badge ${isPass ? 'pass' : 'fail'}">
            ${isPass ? '✅' : '❌'} ${it.key}
          </span>`;
        }).join('')}
      </div>
      <div class="qc-criteria">
        <div class="qc-criterion ${!q.floating_products ? 'pass' : 'fail'}">
          <span class="qc-icon">🎈</span>
          <span class="qc-label">Floating Products</span>
          <span class="qc-status">${!q.floating_products ? 'PASS' : 'FAIL'}</span>
        </div>
        <div class="qc-criterion ${!q.garbled_text ? 'pass' : 'fail'}">
          <span class="qc-icon">🔤</span>
          <span class="qc-label">Garbled Text</span>
          <span class="qc-status">${!q.garbled_text ? 'PASS' : 'FAIL'}</span>
        </div>
        <div class="qc-criterion ${!q.detached_shadows ? 'pass' : 'fail'}">
          <span class="qc-icon">🌑</span>
          <span class="qc-label">Detached Shadows</span>
          <span class="qc-status">${!q.detached_shadows ? 'PASS' : 'FAIL'}</span>
        </div>
        <div class="qc-criterion ${!q.fake_products ? 'pass' : 'fail'}">
          <span class="qc-icon">⚛️</span>
          <span class="qc-label">Fake Products</span>
          <span class="qc-status">${!q.fake_products ? 'PASS' : 'FAIL'}</span>
        </div>
        <div class="qc-criterion ${q.readable_labels ? 'pass' : 'fail'}">
          <span class="qc-icon">🏷️</span>
          <span class="qc-label">Readable Labels</span>
          <span class="qc-status">${q.readable_labels ? 'PASS' : 'FAIL'}</span>
        </div>
      </div>
      ${q.issues && q.issues.length ? `
        <div class="qc-issues">
          <div class="qc-issues-title">Issues Found</div>
          ${q.issues.map(iss => `<div class="qc-issue">⚠ ${iss}</div>`).join('')}
        </div>` : ''}
      <div class="qc-footer">
        <a href="https://github.com/camster91/creative-studio#qc-gate" target="_blank">📖 CLI docs: bash launch.sh qc --input image.png</a>
        <span>Powered by Gemini Vision</span>
      </div>
    `;
    $('qcResults').style.display = 'block';
    showToast('Audit complete');
  } catch(e) {
    showToast('QC failed: ' + e.message, 'err');
  }
}
// -- Export Presets ---------------------------------------------------
// Preset metadata: label, dimensions, and human-readable size string
const EXPORT_PRESETS = {
  'amazon':       { label: 'Amazon',       dims: '2000×2000',  bg: 'white' },
  'shopify':      { label: 'Shopify',      dims: '2048×2048',  bg: 'white' },
  'meta-feed':    { label: 'Meta Feed',    dims: '1080×1350',  bg: 'transparent' },
  'meta-stories': { label: 'Meta Stories',dims: '1080×1920',  bg: 'transparent' },
  'pinterest':    { label: 'Pinterest',    dims: '1000×1500',  bg: 'transparent' },
  'web-hero':     { label: 'Web Hero',     dims: '1920×1080',  bg: 'transparent' },
  'print-dpi':    { label: 'Print',        dims: '300 DPI',    bg: 'white' },
};

const PRESET_LABELS = {
  'amazon': 'Amazon (1:1 White)',
  'shopify': 'Shopify (2048×2048)',
  'meta-feed': 'Meta Feed (4:5)',
  'meta-stories': 'Meta Stories (9:16)',
  'pinterest': 'Pinterest (2:3)',
  'web-hero': 'Web Hero (16:9)',
  'print-dpi': 'Print (300 DPI)',
};

async function exportSelected() {
  const imgUrl = $('mainImg').getAttribute('src');
  if (!imgUrl) { showToast('No image selected', 'err'); return; }
  const checked = Array.from(
    document.querySelectorAll('.dropdown-content input[type="checkbox"]:checked')
  ).map(el => el.value);
  if (!checked.length) { showToast('Select at least one preset', 'err'); return; }
  const btn = $('exportBtn');
  btn.disabled = true;
  btn.textContent = 'Exporting…';
  try {
    const fd = new FormData();
    fd.append('image_url', imgUrl);
    fd.append('presets', checked.join(','));
    fd.append('session_id', state.sessionId || '');
    const resp = await fetch('/api/export', { method: 'POST', body: fd });
    const data = await resp.json();
    if (data.error) throw new Error(data.error);
    // Track presets in image metadata (stored server-side in session entry note)
    if (data.images && data.images.length) {
      renderExportResults(data.images, checked);
    }
    showToast(`Exported ${data.images ? data.images.length : 0} formats`);
  } catch(e) {
    showToast('Export failed: ' + e.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Export Selected';
  }
}

function renderExportResults(images, presets) {
  const container = $('exportResults');
  container.style.display = 'flex';
  const presetSet = new Set(presets);
  container.innerHTML = `
    <div class="export-results-title">Exported Formats</div>
    <div class="export-grid">
      ${images.map((img, i) => {
        const presetKey = presets[i] || 'export';
        const meta = EXPORT_PRESETS[presetKey] || {};
        return `
        <div class="export-card">
          <img src="${img.url}" loading="lazy">
          <div class="export-card-body">
            <div class="export-card-label">${PRESET_LABELS[presetKey] || presetKey}</div>
            <div class="export-card-dims">${meta.dims || ''}</div>
            <a href="${img.url}" download="${img.name}" class="export-card-dl"
               onclick="trackExportDownload('${presetKey}','${img.url}')">
              ⬇ Download
            </a>
          </div>
        </div>`;
      }).join('')}
    </div>
  `;
}

function trackExportDownload(preset, imgUrl) {
  // Fire-and-forget: record preset usage in session entry metadata
  fetch('/api/export-track', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      image_url: imgUrl,
      preset: preset,
      session_id: state.sessionId || null
    })
  }).catch(() => {});
}
async function refreshCost(){
  try{ const r=await fetch('/api/costs'); const d=await r.json();
    $('todayCost').textContent = '$'+(d.total||0).toFixed(2);
  }catch(e){ console.log(e); }
}
refreshCost(); setInterval(refreshCost, 15000);
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
    smart = data.get("smart", False)
    variations = int(data.get("variations", 4))
    session_id = data.get("session_id", new_session_id())
    figma_url = data.get("figma_url")

    # Fetch figma context if requested
    figma_ctx = None
    if figma_url:
        file_key, node_id = parse_figma_url(figma_url)
        if file_key:
            figma_ctx = fetch_figma_context(file_key, node_id)
            if "error" not in figma_ctx:
                prompt = enhance_prompt_with_figma(prompt, figma_ctx)

    images = run_cli_generate(prompt, mode, tier, aspect, smart, variations=variations)
    for img in images:
        if "error" not in img:
            add_entry(
                session_id,
                {
                    "type": mode,
                    "prompt": prompt[:100],
                    "cost": img.get("cost", 0),
                    "image_url": img.get("url", ""),
                    "model": img.get("model", ""),
                    "note": f"{img.get('name', '')} ({img.get('model', '')})",
                },
            )

    costs = load_costs()
    costs["session_count"] = len(list(SESSIONS_DIR.glob("*.json")))
    save_costs(costs)

    return jsonify(
        {
            "message": f"Generated {len(images)} image(s)",
            "images": images,
            "session_id": session_id,
        }
    )


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
        add_entry(
            session_id,
            {
                "type": "composite",
                "prompt": prompt[:100],
                "cost": img.get("cost", 0),
                "image_url": img.get("url", ""),
                "model": img.get("model", ""),
                "note": img.get("name", ""),
            },
        )

    return jsonify(
        {"message": "Composite generated", "images": images, "session_id": session_id}
    )


@app.route("/api/export", methods=["POST"])
def api_export():
    session_id = request.form.get("session_id", new_session_id())
    presets = request.form.get("presets", "")
    if not presets:
        return jsonify({"error": "Presets required"}), 400

    # Case 1: Existing image from URL
    img_url = request.form.get("image_url")
    if img_url and img_url.startswith("/image/"):
        rel_path = img_url.replace("/image/", "")
        src_path = OUTPUT_DIR / rel_path
    # Case 2: Uploaded image
    elif "image" in request.files:
        f = request.files["image"]
        tmp_dir = DATA_DIR / "uploads"
        tmp_dir.mkdir(exist_ok=True)
        src_path = tmp_dir / f"export_{int(time.time())}_{f.filename}"
        f.save(str(src_path))
    else:
        return jsonify({"error": "Image required"}), 400

    images = run_cli_export(str(src_path), presets)
    selected_list = [p.strip() for p in presets.split(",") if p.strip()]
    for img in images:
        add_entry(
            session_id,
            {
                "type": "export",
                "cost": 0,
                "image_url": img.get("url", ""),
                "model": "PIL",
                "note": f"Exported to:[{', '.join(selected_list)}]",
            },
        )

    return jsonify(
        {
            "message": f"Exported to {len(images)} formats",
            "images": images,
            "session_id": session_id,
        }
    )


@app.route("/api/export-track", methods=["POST"])
def api_export_track():
    """Record which presets were used for a given exported image (metadata tracking)."""
    data = request.json or {}
    img_url = data.get("image_url", "")
    preset = data.get("preset", "")
    session_id = data.get("session_id") or None
    if session_id and img_url and preset:
        session_data = load_session(session_id)
        for e in reversed(session_data.get("entries", [])):
            if e.get("image_url") == img_url:
                # Append preset to existing note so we know which export formats were used
                existing_note = e.get("note", "")
                used_presets = []
                if existing_note:
                    m = re.search(r"Exported to:\[(.*?)\]", existing_note)
                    if m:
                        used_presets = [p.strip() for p in m.group(1).split(",")]
                if preset not in used_presets:
                    used_presets.append(preset)
                e["note"] = f"Exported to:[{', '.join(used_presets)}]"
                save_session(session_id, session_data)
    return jsonify({"ok": True})


@app.route("/api/qc", methods=["POST"])
def api_qc():
    # Case 1: Existing image from URL (JSON or Form)
    data = request.json or request.form
    img_url = data.get("image_url")
    if img_url and img_url.startswith("/image/"):
        rel_path = img_url.replace("/image/", "")
        img_path = OUTPUT_DIR / rel_path
    # Case 2: Uploaded image
    elif "image" in request.files:
        f = request.files["image"]
        tmp_dir = DATA_DIR / "uploads"
        tmp_dir.mkdir(exist_ok=True)
        img_path = tmp_dir / f"qc_{int(time.time())}_{f.filename}"
        f.save(str(img_path))
    else:
        return jsonify({"error": "Image required"}), 400

    qc = run_cli_qc(str(img_path))
    return jsonify({"message": f"QC Score: {qc['quality_score']}/10", "qc": qc})


@app.route("/api/figma", methods=["POST"])
def api_figma():
    url = request.json.get("url")
    if not url:
        return jsonify({"error": "URL required"}), 400
    file_key, node_id = parse_figma_url(url)
    if not file_key:
        return jsonify({"error": "Invalid Figma URL"}), 400
    ctx = fetch_figma_context(file_key, node_id)
    return jsonify(ctx)


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
        add_entry(
            session_id,
            {
                "type": "refine",
                "cost": img.get("cost", 0),
                "image_url": img.get("url", ""),
                "model": img.get("model", ""),
                "note": full_changes[:200],
            },
        )

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
    if image_path and not image_path.startswith("/"):
        image_path = "/" + image_path
    return jsonify({"pins": load_pins(image_path)})


@app.route("/api/pins/<path:image_path>/<pin_id>", methods=["DELETE"])
def api_pins_delete(image_path, pin_id):
    if image_path and not image_path.startswith("/"):
        image_path = "/" + image_path
    pins = [p for p in load_pins(image_path) if p.get("id") != pin_id]
    save_pins(image_path, pins)
    return jsonify({"pins": pins})


@app.route("/api/pins/<path:image_path>", methods=["DELETE"])
def api_pins_clear(image_path):
    if image_path and not image_path.startswith("/"):
        image_path = "/" + image_path
    save_pins(image_path, [])
    return jsonify({"pins": []})


@app.route("/api/sessions", methods=["GET"])
def api_sessions():
    sessions = []
    for p in sorted(
        SESSIONS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True
    ):
        data = load_json(p)
        sessions.append(
            {
                "id": data.get("id", p.stem),
                "created_at": data.get("created_at", ""),
                "entries": data.get("entries", []),
                "cost": sum(e.get("cost", 0) for e in data.get("entries", [])),
            }
        )
    return jsonify({"sessions": sessions})


@app.route("/api/session/<session_id>", methods=["GET"])
def api_session_get(session_id):
    data = load_session(session_id)
    entries = []
    for e in data.get("entries", []):
        e2 = dict(e)
        e2["image_url"] = e.get("image_url", "")
        entries.append(e2)
    return jsonify(
        {"id": session_id, "entries": entries, "created_at": data.get("created_at", "")}
    )


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
