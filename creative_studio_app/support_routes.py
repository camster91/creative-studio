"""Waitlist, template, runtime-status, and operator Flask routes."""

import csv
import html
import io
import time
from collections.abc import Callable

from flask import Blueprint, jsonify, request


def create_blueprint(
    *,
    waitlist_pattern,
    request_lock,
    read_waitlist: Callable[[], list],
    write_waitlist: Callable[[list], None],
    now: Callable[[], str],
    read_templates: Callable[[], list],
    server_api_key: str,
    allow_server_fallback: bool,
    version: str,
    admin_authed: Callable[[], bool],
    rate_limited: Callable,
) -> Blueprint:
    blueprint = Blueprint("support", __name__)

    @blueprint.post("/api/waitlist")
    @rate_limited
    def waitlist_signup():
        data = request.json or {}
        email = (data.get("email") or "").strip().lower()
        if not email or len(email) > 320 or not waitlist_pattern.match(email):
            return jsonify({"error": "Invalid email"}), 400
        source = str(data.get("source") or "unknown")[:64]
        with request_lock:
            entries = read_waitlist()
            for position, entry in enumerate(entries, start=1):
                if entry.get("email") == email:
                    return jsonify(
                        {
                            "email": email,
                            "already_signed_up": True,
                            "position": position,
                            "total_signups": len(entries),
                        }
                    )
            entries.append({"email": email, "source": source, "ts": now()})
            write_waitlist(entries)
            return jsonify(
                {
                    "email": email,
                    "position": len(entries),
                    "total_signups": len(entries),
                }
            ), 201

    @blueprint.get("/api/templates")
    @rate_limited
    def templates():
        return jsonify({"templates": read_templates(), "version": 1})

    @blueprint.get("/api/whoami")
    @rate_limited
    def whoami():
        user_key = request.headers.get("X-API-Key", "").strip()
        fallback_enabled = bool(allow_server_fallback and server_api_key)
        return jsonify(
            {
                "byok": bool(user_key),
                "user_supplied_key": bool(user_key),
                "server_has_fallback": bool(server_api_key),
                "fallback_enabled": fallback_enabled,
                "byok_required": not fallback_enabled,
                "version": version,
            }
        )

    @blueprint.get("/admin/waitlist")
    def admin_waitlist():
        if not admin_authed():
            return (
                "<h1>401</h1><p>Set PHOTOGEN_ADMIN_SECRET and pass it as "
                "X-Admin-Secret to see the waitlist.</p>",
                401,
                {"Content-Type": "text/html; charset=utf-8"},
            )
        entries = read_waitlist()
        sources = {}
        for entry in entries:
            source = str(entry.get("source", "unknown"))
            sources[source] = sources.get(source, 0) + 1
        rows = "\n".join(
            "<tr><td>{}</td><td><code>{}</code></td><td>{}</td></tr>".format(
                html.escape(str(entry.get("ts", "?"))),
                html.escape(str(entry.get("email", "?"))),
                html.escape(str(entry.get("source", "?"))),
            )
            for entry in entries
        )
        source_rows = "\n".join(
            f"<tr><td>{html.escape(source)}</td><td>{count}</td></tr>"
            for source, count in sorted(sources.items(), key=lambda item: -item[1])
        )
        page = f"""<!doctype html><html><head><meta charset="utf-8"><title>Photogen — Waitlist</title>
<style>body{{font:14px/1.5 -apple-system,sans-serif;margin:24px;color:#1a1a1a;background:#fafafa}} table{{border-collapse:collapse;width:100%;max-width:1100px;background:#fff}} th,td{{padding:8px 12px;text-align:left;border-bottom:1px solid #eee}} th{{background:#f5f5f5}}</style></head>
<body><h1>Photogen Waitlist</h1><p><b>{len(entries)}</b> total signups — <a href="/admin/waitlist.csv?ts={int(time.time())}">Download CSV</a></p>
<h2>By source</h2><table><tr><th>Source</th><th>Signups</th></tr>{source_rows}</table>
<h2>All signups (newest first)</h2><table><tr><th>Timestamp</th><th>Email</th><th>Source</th></tr>{rows or '<tr><td colspan="3"><i>No signups yet.</i></td></tr>'}</table></body></html>"""
        return page, 200, {"Content-Type": "text/html; charset=utf-8"}

    @blueprint.get("/admin/waitlist.csv")
    def admin_waitlist_csv():
        if not admin_authed():
            return "401", 401, {"Content-Type": "text/plain"}
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(["timestamp", "email", "source"])
        for entry in read_waitlist():
            writer.writerow(
                [entry.get("ts", ""), entry.get("email", ""), entry.get("source", "")]
            )
        return buffer.getvalue(), 200, {
            "Content-Type": "text/csv; charset=utf-8",
            "Content-Disposition": 'attachment; filename="photogen-waitlist.csv"',
        }

    return blueprint
