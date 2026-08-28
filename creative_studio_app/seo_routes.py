"""Public robots, sitemap, and blog Flask blueprint."""

import html
import json
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path

from flask import Blueprint

from .seo import canonical_url, load_blog_posts, parse_blog_post


def create_blueprint(get_content_dir: Callable[[], Path]) -> Blueprint:
    blueprint = Blueprint("seo", __name__)

    @blueprint.get("/robots.txt")
    def robots_txt():
        return (
            "User-agent: *\nAllow: /\nDisallow: /admin/\nDisallow: /api/\n"
            f"Sitemap: {canonical_url('/sitemap.xml')}\n",
            200,
            {"Content-Type": "text/plain; charset=utf-8"},
        )

    @blueprint.get("/sitemap.xml")
    def sitemap_xml():
        urls = [("/", "weekly", "1.0"), ("/blog", "weekly", "0.9"), ("/privacy", "monthly", "0.3")]
        urls += [(f"/blog/{post['slug']}", "monthly", "0.7") for post in load_blog_posts(get_content_dir())]
        root = ET.Element("urlset", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")
        for path, frequency, priority in urls:
            url = ET.SubElement(root, "url")
            ET.SubElement(url, "loc").text = canonical_url(path)
            ET.SubElement(url, "changefreq").text = frequency
            ET.SubElement(url, "priority").text = priority
        xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")
        return xml, 200, {"Content-Type": "application/xml; charset=utf-8"}

    @blueprint.get("/blog")
    def blog_index():
        posts = load_blog_posts(get_content_dir())
        items = "".join(
            f'<li><a href="/blog/{html.escape(post["slug"])}">{html.escape(post["title"])}</a>'
            f'<p>{html.escape(post["date"])} &middot; {html.escape(post["description"])}</p></li>'
            for post in posts
        ) or "<li><i>No posts yet. Check back soon.</i></li>"
        page = f"""<!doctype html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Blog — Photogen</title><meta name="description" content="Practical guides on AI product photography, CPG/DTC creative, and shipping ad campaigns faster.">
<link rel="canonical" href="{canonical_url('/blog')}"><meta property="og:title" content="Photogen Blog">
<meta property="og:description" content="Practical guides on AI product photography and CPG/DTC creative.">
<meta property="og:type" content="website"><meta property="og:url" content="{canonical_url('/blog')}">
<style>body{{max-width:720px;margin:40px auto;padding:0 24px;font:16px/1.6 system-ui}}li{{padding:16px 0}}a{{font-weight:600}}</style></head>
<body><p><a href="/">&larr; Photogen</a></p><h1>Blog</h1><p>Practical guides on AI product photography, CPG/DTC creative, and shipping ad campaigns faster.</p><ul>{items}</ul></body></html>"""
        return page, 200, {"Content-Type": "text/html; charset=utf-8"}

    @blueprint.get("/blog/<slug>")
    def blog_post(slug):
        post = parse_blog_post(get_content_dir() / f"{slug}.md")
        if not post:
            return f"<h1>Not found</h1><p>No post named {html.escape(slug)!r}.</p>", 404, {"Content-Type": "text/html; charset=utf-8"}
        structured = json.dumps({
            "@context": "https://schema.org", "@type": "Article",
            "headline": post["title"], "description": post["description"],
            "datePublished": post["date"],
            "author": {"@type": "Organization", "name": "Photogen"},
            "publisher": {"@type": "Organization", "name": "Photogen"},
        }).replace("</", "<\\/")
        call_to_action = (
            f'<p class="cta"><a href="/app?template={html.escape(post["template_id"])}">Try this template in Photogen →</a></p>'
            if post.get("template_id") else ""
        )
        tags = "".join(f"<span>{html.escape(tag)}</span> " for tag in post["tags"])
        page = f"""<!doctype html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(post['title'])} — Photogen Blog</title>
<meta name="description" content="{html.escape(post['description'])}"><link rel="canonical" href="{canonical_url('/blog/' + post['slug'])}">
<meta property="og:title" content="{html.escape(post['title'])}"><meta property="og:description" content="{html.escape(post['description'])}">
<meta property="og:type" content="article"><meta property="og:url" content="{canonical_url('/blog/' + post['slug'])}">
<script type="application/ld+json">{structured}</script><style>body{{max-width:720px;margin:40px auto;padding:0 24px;font:16px/1.7 system-ui}}.cta{{padding:20px;background:#f2f0ff}}</style></head>
<body><p><a href="/blog">&larr; All posts</a></p><h1>{html.escape(post['title'])}</h1><p>{html.escape(post['date'])}</p>
<div>{post['body_html']}</div>{call_to_action}<p>{tags}</p></body></html>"""
        return page, 200, {"Content-Type": "text/html; charset=utf-8"}

    return blueprint
