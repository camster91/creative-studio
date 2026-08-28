"""Operational status, API reference, and session-history page rendering."""

import html
import time
from datetime import datetime
from pathlib import Path
from typing import Callable


PAGE_STYLE = """
:root{--bg:#0a0a0f;--surface:#14141b;--border:#2a2a33;--text:#f0f0f5;--muted:#9a9aa8;--ok:#2dd4a8;--warn:#fbbf24;--accent:#ff6b4a}
*{box-sizing:border-box}body{font:15px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;background:var(--bg);color:var(--text);max-width:880px;margin:0 auto;padding:40px 24px}
a{color:var(--accent)}h1{font-size:1.4rem}.muted{color:var(--muted)}.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:20px;margin:16px 0}
.row{display:flex;justify-content:space-between;gap:20px;padding:9px 0;border-bottom:1px solid var(--border)}.row:last-child{border:0}.ok{color:var(--ok)}.warn{color:var(--warn)}
code,pre{font-family:ui-monospace,monospace}pre{overflow:auto;background:#0d0d12;padding:12px;border-radius:8px}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px;border-bottom:1px solid var(--border)}
"""


def page(title: str, body: str, *, refresh_seconds: int | None = None) -> str:
    refresh = (
        f'<script>setTimeout(()=>location.reload(),{refresh_seconds * 1000});</script>'
        if refresh_seconds
        else ""
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)} | Creative Studio</title><style>{PAGE_STYLE}</style>"
        f"</head><body>{body}{refresh}</body></html>"
    )


def render_status(costs: dict, jobs: dict[str, dict]) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    spent_today = float(costs.get("by_date", {}).get(today, 0.0))
    active = [
        (identifier, job)
        for identifier, job in list(jobs.items())[-20:]
        if job.get("status") == "running"
    ]
    job_rows = "".join(
        '<div class="row"><code>'
        + html.escape(identifier[:20])
        + "</code><span class=\"warn\">running "
        + f"{max(0, time.time() - float(job.get('started_at') or time.time())):.1f}s"
        + "</span></div>"
        for identifier, job in active
    ) or '<div class="row"><span>No active jobs</span><span class="ok">idle</span></div>'
    body = f"""
    <nav><a href="/">Studio</a> · <a href="/docs">API Docs</a> · <a href="/history">History</a></nav>
    <h1>Creative Studio Status</h1><p class="muted">{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC</p>
    <section class="card"><h2>Cost tracker</h2>
      <div class="row"><span>Today</span><strong class="{'warn' if spent_today > 3 else 'ok'}">${spent_today:.2f}</strong></div>
      <div class="row"><span>All time</span><strong>${float(costs.get('total', 0)):.2f}</strong></div>
      <div class="row"><span>Images generated</span><strong>{int(costs.get('image_count', 0))}</strong></div>
    </section><section class="card"><h2>Active jobs ({len(active)})</h2>{job_rows}</section>
    <p class="muted">Auto-refreshes every 30 seconds.</p>"""
    return page("Status", body, refresh_seconds=30)


def render_docs() -> str:
    endpoints = [
        ("GET", "/api/whoami", "Report BYOK/server-key mode."),
        ("POST", "/api/validate-key", "Validate a Gemini key without storing it."),
        ("POST", "/api/generate", "Start image generation and return a job ID."),
        ("GET", "/api/jobs/<job_id>", "Read an authenticated job result."),
        ("POST", "/api/composite", "Place an uploaded product in a generated scene."),
        ("POST", "/api/export", "Export an output using platform presets."),
        ("POST", "/api/qc", "Evaluate an output against the visual QC rubric."),
        ("GET", "/image/<path>", "Serve an immutable generated image."),
    ]
    rows = "".join(
        f"<tr><td><strong>{method}</strong></td><td><code>{html.escape(path)}</code></td><td>{html.escape(description)}</td></tr>"
        for method, path, description in endpoints
    )
    body = f"""
    <nav><a href="/">Studio</a> · <a href="/status">Status</a> · <a href="/history">History</a></nav>
    <h1>Creative Studio API</h1>
    <p>Generation endpoints use bring-your-own-key authentication. Send the Gemini key in <code>X-API-Key</code>; it is not persisted.</p>
    <section class="card"><h2>Example</h2><pre>curl -X POST -H "X-API-Key: YOUR_KEY" -H "Content-Type: application/json" \\
  -d '{{"prompt":"A product on marble","tier":"balanced"}}' \\
  https://photogen.ashbi.ca/api/generate</pre></section>
    <section class="card"><h2>Endpoints</h2><table><thead><tr><th>Method</th><th>Path</th><th>Purpose</th></tr></thead><tbody>{rows}</tbody></table></section>
    """
    return page("API Docs", body)


def load_history(sessions_dir: Path, load_json: Callable) -> list[dict]:
    sessions = []
    if not sessions_dir.exists():
        return sessions
    files = sorted(sessions_dir.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for session_file in files[:100]:
        try:
            data = load_json(session_file)
            images = data.get("images", data.get("entries", []))
            first = images[0] if images else {}
            prompt = str(first.get("prompt", ""))
            sessions.append({
                "id": session_file.stem,
                "created": datetime.fromtimestamp(session_file.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                "images": len(images),
                "cost": sum(float(item.get("cost", 0) or 0) for item in images),
                "model": str(first.get("model", "unknown")),
                "prompt": prompt[:60] + ("..." if len(prompt) > 60 else ""),
            })
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return sessions


def render_history(sessions_dir: Path, load_json: Callable) -> str:
    sessions = load_history(sessions_dir, load_json)
    rows = "".join(
        "<tr>"
        f"<td>{html.escape(session['created'])}</td>"
        f"<td><code>{html.escape(session['id'][:16])}</code></td>"
        f"<td>{html.escape(session['model'])}</td><td>{session['images']}</td>"
        f"<td>${session['cost']:.2f}</td><td>{html.escape(session['prompt'])}</td></tr>"
        for session in sessions
    ) or '<tr><td colspan="6">No sessions yet.</td></tr>'
    body = f"""
    <nav><a href="/">Studio</a> · <a href="/status">Status</a> · <a href="/docs">API Docs</a></nav>
    <h1>Generation history</h1><section class="card"><table><thead><tr><th>Date</th><th>Session</th><th>Model</th><th>Images</th><th>Cost</th><th>Prompt</th></tr></thead><tbody>{rows}</tbody></table></section>
    """
    return page("History", body)
