"""Blog content parsing, safe Markdown rendering, and canonical URLs."""

import html
import re
from pathlib import Path


FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def inline_markdown(value: str) -> str:
    """Render the deliberately small set of supported inline Markdown."""
    value = html.escape(value)
    value = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', value)
    value = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", value)
    value = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", value)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", value)


def markdown_to_html(markdown: str) -> str:
    """Render safe headings, paragraphs, lists, links, and code blocks."""
    output = []
    in_code = False
    in_list = False
    for line in markdown.splitlines():
        if line.startswith("```"):
            if in_code:
                output.append("</code></pre>")
                in_code = False
            else:
                output.append("<pre><code>")
                in_code = True
            continue
        if in_code:
            output.append(html.escape(line))
            continue
        if line.startswith("### "):
            if in_list:
                output.append("</ul>")
                in_list = False
            output.append(f"<h3>{html.escape(line[4:].strip())}</h3>")
        elif line.startswith("## "):
            if in_list:
                output.append("</ul>")
                in_list = False
            output.append(f"<h2>{html.escape(line[3:].strip())}</h2>")
        elif line.startswith("# "):
            if in_list:
                output.append("</ul>")
                in_list = False
            output.append(f"<h1>{html.escape(line[2:].strip())}</h1>")
        elif line.startswith("- "):
            if not in_list:
                output.append("<ul>")
                in_list = True
            output.append(f"<li>{inline_markdown(line[2:].strip())}</li>")
        elif not line.strip():
            if in_list:
                output.append("</ul>")
                in_list = False
            output.append("")
        else:
            if in_list:
                output.append("</ul>")
                in_list = False
            output.append(f"<p>{inline_markdown(line)}</p>")
    if in_list:
        output.append("</ul>")
    if in_code:
        output.append("</code></pre>")
    return "\n".join(output)


def parse_blog_post(path: Path) -> dict:
    """Parse frontmatter and sanitized Markdown from a blog source file."""
    try:
        raw = path.read_text()
    except (OSError, UnicodeDecodeError):
        return {}
    match = FRONTMATTER_RE.match(raw)
    if not match:
        return {}
    frontmatter, body_markdown = match.group(1), match.group(2)
    metadata = {}
    for line in frontmatter.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            metadata[key.strip().lower()] = value.strip().strip('"').strip("'")
    slug = path.stem
    return {
        "slug": slug,
        "title": metadata.get("title", slug.replace("-", " ").title()),
        "description": metadata.get("description", ""),
        "date": metadata.get("date", ""),
        "tags": [tag.strip() for tag in metadata.get("tags", "").split(",") if tag.strip()],
        "template_id": metadata.get("template_id", "").strip(),
        "body_md": body_markdown,
        "body_html": markdown_to_html(body_markdown),
    }


def load_blog_posts(content_dir: Path) -> list[dict]:
    if not content_dir.is_dir():
        return []
    posts = [parse_blog_post(path) for path in content_dir.glob("*.md")]
    posts = [post for post in posts if post]
    return sorted(posts, key=lambda post: post.get("date") or "", reverse=True)


def canonical_url(path: str, origin: str = "https://photogen.ashbi.ca") -> str:
    return origin.rstrip("/") + "/" + path.lstrip("/") if path else origin.rstrip("/")
