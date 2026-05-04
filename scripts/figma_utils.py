"""Figma design-aware generation helpers."""

import json
import os
import re
import urllib.error
import urllib.request


def parse_figma_url(url: str) -> tuple:
    """Extract file_key and optional node_id from a Figma URL."""
    m = re.search(r"figma\.com/(?:design|file)/([a-zA-Z0-9]+)", url)
    file_key = m.group(1) if m else None
    n = re.search(r"node-id=([0-9\-]+)", url)
    node_id = n.group(1) if n else None
    return file_key, node_id


def _figma_api(path: str) -> dict:
    """Call Figma REST API and return JSON."""
    token = os.environ.get("FIGMA_ACCESS_TOKEN", "")
    if not token:
        return {"error": "FIGMA_ACCESS_TOKEN not set"}
    url = f"https://api.figma.com/v1{path}"
    req = urllib.request.Request(url, headers={"X-Figma-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode()[:500]}
    except Exception as e:
        return {"error": str(e)}


def fetch_figma_context(file_key: str, node_id: str = None) -> dict:
    """Read a Figma file (or specific node) and extract design context tokens."""
    data = _figma_api(
        f"/files/{file_key}/nodes?ids={node_id}" if node_id else f"/files/{file_key}"
    )

    def extract(node):
        ctx = {
            "fills": [],
            "strokes": [],
            "fonts": [],
            "effects": [],
            "layout": [],
            "names": [],
        }
        stack = [node]
        while stack:
            n = stack.pop()
            if not isinstance(n, dict):
                continue
            ctx["names"].append(n.get("name", ""))
            t = n.get("type", "")
            if t == "TEXT":
                style = n.get("style", {})
                font = style.get("fontFamily", "")
                if font:
                    ctx["fonts"].append(font)
            if "fills" in n:
                for f in n.get("fills", []):
                    if isinstance(f, dict) and "color" in f:
                        c = f["color"]
                        rgb = "#{:02X}{:02X}{:02X}".format(
                            int(c.get("r", 0) * 255),
                            int(c.get("g", 0) * 255),
                            int(c.get("b", 0) * 255),
                        )
                        ctx["fills"].append(rgb)
            if "strokes" in n:
                for s in n.get("strokes", []):
                    if isinstance(s, dict) and "color" in s:
                        c = s["color"]
                        rgb = "#{:02X}{:02X}{:02X}".format(
                            int(c.get("r", 0) * 255),
                            int(c.get("g", 0) * 255),
                            int(c.get("b", 0) * 255),
                        )
                        ctx["strokes"].append(rgb)
            if "effects" in n:
                ctx["effects"].extend(
                    [
                        e.get("type", "")
                        for e in n.get("effects", [])
                        if isinstance(e, dict)
                    ]
                )
            if "layoutMode" in n:
                ctx["layout"].append(n["layoutMode"])
            for child in n.get("children", []):
                stack.append(child)
        return ctx

    if node_id and "nodes" in data:
        doc = data["nodes"].get(node_id.replace("-", ":"), {}).get("document", {})
    elif "document" in data:
        doc = data["document"]
    else:
        return {
            "error": data.get("error", "No document found"),
            "raw_response": json.dumps(data)[:200],
        }

    ctx = extract(doc)
    # Deduplicate and trim
    for k in ctx:
        seen = []
        uniq = []
        for v in ctx[k]:
            if v not in seen:
                seen.append(v)
                uniq.append(v)
        ctx[k] = uniq[:8]
    return ctx


def enhance_prompt_with_figma(brief: str, ctx: dict) -> str:
    """Merge the user's brief with extracted Figma design context."""
    parts = []
    if brief:
        parts.append(brief)
    if ctx.get("fills"):
        parts.append("Color palette (from Figma): " + ", ".join(ctx["fills"][:5]) + ".")
    if ctx.get("fonts"):
        parts.append("Typography (from Figma): " + ", ".join(ctx["fonts"][:3]) + ".")
    if ctx.get("layout"):
        parts.append("Layout style: " + ", ".join(set(ctx["layout"])) + ".")
    if ctx.get("effects"):
        parts.append("Effects: " + ", ".join(ctx["effects"]) + ".")
    return " ".join(parts)


def post_figma_comment(file_key: str, node_id: str, message: str) -> dict:
    """Post a comment on a Figma node to 'insert' the generated result."""
    token = os.environ.get("FIGMA_ACCESS_TOKEN", "")
    if not token:
        return {"error": "FIGMA_ACCESS_TOKEN not set"}

    url = f"https://api.figma.com/v1/files/{file_key}/comments"
    payload = json.dumps({"message": message, "client_meta": {"x": 0, "y": 0}}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"X-Figma-Token": token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode()[:500]}
    except Exception as e:
        return {"error": str(e)}
