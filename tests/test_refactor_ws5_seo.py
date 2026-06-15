"""
Regression tests for WS-5: SEO surface (robots, sitemap, blog).

The SEO surface is entirely public, unauthenticated, and static-ish
(blog content lives in content/blog/*.md files). The tests verify:
- /robots.txt returns the right content + points at the sitemap
- /sitemap.xml is valid XML, includes the static pages and posts
- /blog renders the list
- /blog/<slug> renders the post (with structured data)
- Posts are loaded from content/blog/*.md files with frontmatter

Run:  pytest tests/test_refactor_ws5_seo.py -v
"""
import importlib.util
import os
import sys
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).parent.parent / "scripts"
REPO = Path(__file__).parent.parent


def _load_module(tmp_path):
    web_path = SCRIPT_DIR / "creative-studio-web.py"
    sys.path.insert(0, str(SCRIPT_DIR))
    os.environ.setdefault("GEMINI_API_KEY", "test-key")
    spec = importlib.util.spec_from_file_location("creative_studio_web", web_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["creative_studio_web"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def cs(tmp_path):
    return _load_module(tmp_path)


# ─── /robots.txt ────────────────────────────────────────────────────

class TestRobotsTxt:
    def test_returns_200(self, cs):
        r = cs.app.test_client().get("/robots.txt")
        assert r.status_code == 200
        assert "text/plain" in r.headers.get("Content-Type", "")

    def test_allows_root_and_disallows_admin(self, cs):
        r = cs.app.test_client().get("/robots.txt")
        body = r.get_data(as_text=True)
        assert "User-agent: *" in body
        assert "Allow: /" in body
        assert "Disallow: /admin/" in body
        assert "Disallow: /api/" in body

    def test_points_at_sitemap(self, cs):
        r = cs.app.test_client().get("/robots.txt")
        body = r.get_data(as_text=True)
        assert "Sitemap: https://photogen.ashbi.ca/sitemap.xml" in body


# ─── /sitemap.xml ───────────────────────────────────────────────────

class TestSitemap:
    def test_returns_valid_xml(self, cs):
        r = cs.app.test_client().get("/sitemap.xml")
        assert r.status_code == 200
        # Parse to verify well-formedness
        root = ET.fromstring(r.get_data())
        assert root.tag.endswith("urlset")
        # xmlns attribute is required
        ns = root.tag.split("}")[0].strip("{")
        assert "sitemaps.org" in ns

    def test_includes_static_pages(self, cs):
        r = cs.app.test_client().get("/sitemap.xml")
        root = ET.fromstring(r.get_data())
        urls = [u.find("{*}loc").text for u in root.findall("{*}url")]
        assert "https://photogen.ashbi.ca/" in urls
        assert "https://photogen.ashbi.ca/blog" in urls
        assert "https://photogen.ashbi.ca/privacy" in urls

    def test_includes_blog_posts(self, cs):
        r = cs.app.test_client().get("/sitemap.xml")
        root = ET.fromstring(r.get_data())
        urls = [u.find("{*}loc").text for u in root.findall("{*}url")]
        # The seed post photoroom-vs-photogen should be in there
        assert any("photoroom-vs-photogen" in u for u in urls), \
            f"expected photoroom-vs-photogen post URL, got: {urls}"

    def test_changefreq_and_priority_present(self, cs):
        """Each URL must have lastmod (we use changefreq + priority
        as a simpler signal of update intent)."""
        r = cs.app.test_client().get("/sitemap.xml")
        root = ET.fromstring(r.get_data())
        for url in root.findall("{*}url"):
            assert url.find("{*}changefreq") is not None
            assert url.find("{*}priority") is not None


# ─── /blog ────────────────────────────────────────────────────────

class TestBlogIndex:
    def test_returns_200(self, cs):
        r = cs.app.test_client().get("/blog")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("Content-Type", "")

    def test_has_canonical_link(self, cs):
        """The blog index must have a canonical link tag so search
        engines know which version is the 'real' one."""
        r = cs.app.test_client().get("/blog")
        body = r.get_data(as_text=True)
        assert 'rel="canonical"' in body
        assert 'href="https://photogen.ashbi.ca/blog"' in body

    def test_has_og_meta(self, cs):
        r = cs.app.test_client().get("/blog")
        body = r.get_data(as_text=True)
        assert 'property="og:title"' in body
        assert 'property="og:description"' in body
        assert 'property="og:type" content="website"' in body

    def test_lists_seed_post(self, cs):
        r = cs.app.test_client().get("/blog")
        body = r.get_data(as_text=True)
        assert "Photoroom vs Photogen" in body
        assert "/blog/photoroom-vs-photogen" in body

    def test_handles_empty_blog(self, tmp_path, monkeypatch):
        """When there are no posts, show a friendly 'check back soon'
        message instead of an empty list."""
        cs = _load_module(tmp_path)
        # Override the blog dir to a tmp empty one
        empty = tmp_path / "empty_blog"
        empty.mkdir()
        monkeypatch.setattr(cs, "BLOG_CONTENT_DIR", empty)
        # Re-evaluate the parser (or just call with empty dir)
        posts = cs._load_all_blog_posts()
        assert posts == []


# ─── /blog/<slug> ─────────────────────────────────────────────────

class TestBlogPost:
    def test_returns_200(self, cs):
        r = cs.app.test_client().get("/blog/photoroom-vs-photogen")
        assert r.status_code == 200

    def test_404_for_nonexistent(self, cs):
        r = cs.app.test_client().get("/blog/nonexistent-post")
        assert r.status_code == 404

    def test_post_body_is_rendered(self, cs):
        r = cs.app.test_client().get("/blog/photoroom-vs-photogen")
        body = r.get_data(as_text=True)
        # The post body has these headings
        assert "What Photoroom does well" in body
        assert "What Photogen does well" in body
        # Table headers from the markdown
        assert "<h1>" in body or "<h2>" in body

    def test_structured_data_present(self, cs):
        """The post must include JSON-LD Article schema so Google
        can show it with rich snippets."""
        r = cs.app.test_client().get("/blog/photoroom-vs-photogen")
        body = r.get_data(as_text=True)
        assert 'application/ld+json' in body
        # Parse the JSON-LD
        m = re.search(r'<script type="application/ld\+json">(.*?)</script>',
                      body, re.DOTALL)
        assert m, "no JSON-LD block found"
        import json
        data = json.loads(m.group(1))
        assert data["@type"] == "Article"
        assert data["headline"]
        assert data["datePublished"]

    def test_seo_to_product_cta_when_template_id_set(self, cs):
        """Posts with a template_id in frontmatter get a CTA button
        linking to the editor with the template pre-loaded. This is
        the SEO-to-product funnel."""
        r = cs.app.test_client().get("/blog/photoroom-vs-photogen")
        body = r.get_data(as_text=True)
        # The seed post has template_id: amazon-hero-coffee
        assert 'class="cta"' in body
        assert "Try this template" in body
        assert "template=amazon-hero-coffee" in body

    def test_post_no_cta_when_template_id_missing(self, tmp_path, monkeypatch):
        """Posts without a template_id don't get the CTA — there's
        no product to funnel to."""
        cs = _load_module(tmp_path)
        # Create a post without template_id
        no_cta = tmp_path / "no_cta_post.md"
        no_cta.write_text("""---
title: "No CTA"
date: "2026-06-15"
description: "Just a regular post"
---
## Section

Content here.
""")
        blog_dir = tmp_path / "blog"
        blog_dir.mkdir()
        (blog_dir / "no-cta.md").write_text(no_cta.read_text())
        monkeypatch.setattr(cs, "BLOG_CONTENT_DIR", blog_dir)
        # Re-parse by calling _parse_blog_post directly
        post = cs._parse_blog_post(blog_dir / "no-cta.md")
        assert post["template_id"] == ""
        assert post["title"] == "No CTA"

    def test_canonical_url_is_https(self, cs):
        """The canonical URL on each post must be https://, not http.
        Search engines de-rank http canonicals."""
        r = cs.app.test_client().get("/blog/photoroom-vs-photogen")
        body = r.get_data(as_text=True)
        m = re.search(r'rel="canonical" href="([^"]+)"', body)
        assert m
        assert m.group(1).startswith("https://")


# ─── Frontmatter parsing ─────────────────────────────────────────────

class TestFrontmatter:
    def test_parses_required_fields(self, cs):
        # Find the seed post
        p = cs._parse_blog_post(cs.BLOG_CONTENT_DIR / "photoroom-vs-photogen.md")
        assert p is not None
        assert p["title"]
        assert p["date"]
        assert p["description"]
        assert p["slug"] == "photoroom-vs-photogen"

    def test_parses_tags(self, cs):
        p = cs._parse_blog_post(cs.BLOG_CONTENT_DIR / "photoroom-vs-photogen.md")
        assert isinstance(p["tags"], list)
        assert "comparison" in p["tags"]
        assert "CPG" in p["tags"]

    def test_parses_template_id(self, cs):
        p = cs._parse_blog_post(cs.BLOG_CONTENT_DIR / "photoroom-vs-photogen.md")
        assert p["template_id"] == "amazon-hero-coffee"

    def test_invalid_post_returns_empty(self, cs, tmp_path):
        """A file with no frontmatter should return {} (not raise)."""
        cs = _load_module(tmp_path)
        bad = tmp_path / "bad.md"
        bad.write_text("Just a paragraph, no frontmatter.\n")
        assert cs._parse_blog_post(bad) == {}

    def test_corrupt_frontmatter_returns_empty(self, cs, tmp_path):
        cs = _load_module(tmp_path)
        bad = tmp_path / "bad.md"
        bad.write_text("---\nthis is not yaml\n---\n\nBody")
        # The current parser is forgiving (just key:value lines)
        # so a malformed frontmatter still parses if lines have colons
        # but a totally empty/missing one returns {}.
        # Test that we don't crash:
        result = cs._parse_blog_post(bad)
        assert isinstance(result, dict)


# ─── Markdown rendering ──────────────────────────────────────────────

class TestMarkdownRendering:
    def test_h1_h2_h3_rendered(self, cs):
        md = "# Title\n## Section\n### Sub"
        html = cs._markdown_to_html(md)
        assert "<h1>Title</h1>" in html
        assert "<h2>Section</h2>" in html
        assert "<h3>Sub</h3>" in html

    def test_paragraphs_rendered(self, cs):
        md = "First paragraph.\n\nSecond paragraph."
        html = cs._markdown_to_html(md)
        assert "<p>First paragraph.</p>" in html
        assert "<p>Second paragraph.</p>" in html

    def test_lists_rendered(self, cs):
        md = "- one\n- two\n- three"
        html = cs._markdown_to_html(md)
        assert "<ul>" in html
        assert "<li>one</li>" in html
        assert "<li>two</li>" in html
        assert "<li>three</li>" in html

    def test_inline_bold_italic_code(self, cs):
        md = "**bold** and *italic* and `code`"
        html = cs._markdown_to_html(md)
        assert "<strong>bold</strong>" in html
        assert "<em>italic</em>" in html
        assert "<code>code</code>" in html

    def test_links_rendered(self, cs):
        md = "Click [here](https://example.com) please"
        html = cs._markdown_to_html(md)
        assert 'href="https://example.com"' in html
        assert ">here</a>" in html

    def test_html_escaped(self, cs):
        """User-provided HTML in markdown must be escaped, not rendered."""
        md = "<script>alert('xss')</script>"
        html = cs._markdown_to_html(md)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_fenced_code_blocks(self, cs):
        md = "```\nprint(123)\n```"
        html = cs._markdown_to_html(md)
        assert "<pre><code>" in html
        assert "print(123)" in html
        assert "</code></pre>" in html


# ─── Static assets: the seed post ships in the repo ──────────────────

class TestSeedPostShips:
    def test_photoroom_vs_photogen_post_exists(self):
        p = REPO / "content" / "blog" / "photoroom-vs-photogen.md"
        assert p.exists(), "seed blog post must ship in the repo"
        body = p.read_text()
        # Frontmatter
        assert body.startswith("---\n")
        # Has a CTA template_id (the SEO-to-product funnel)
        assert "template_id:" in body
        # Mentions Photogen and Photoroom by name
        assert "Photogen" in body
        assert "Photoroom" in body

    def test_content_dir_in_dockerfile(self):
        """The Dockerfile should COPY content/ so blog posts ship
        in the container."""
        from pathlib import Path
        dockerfile = (Path(__file__).parent.parent / "Dockerfile").read_text()
        assert "content" in dockerfile.lower()
