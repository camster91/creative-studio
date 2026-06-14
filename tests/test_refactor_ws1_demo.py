"""
Regression tests for the hero demo block (the animated scene-set
visualization on the landing page).

The demo block is a key part of the WS-1 ship — it shows users what
"scene-set" actually means without a real product photo. The animation
is pure CSS (no JS, no assets) so the test surface is: the right
markup, the right CSS classes, the right reduced-motion behavior.

Run:  pytest tests/test_refactor_ws1_demo.py -v
"""
import re
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).parent.parent


class TestHeroDemoMarkup:
    """The demo block is a CSS-only animation. Verify it's wired up
    correctly in the landing HTML."""

    def test_hero_demo_block_present(self):
        src = (REPO / "templates" / "landing.html").read_text()
        assert 'class="hero-demo"' in src, "missing .hero-demo block"

    def test_five_scene_tiles_present(self):
        src = (REPO / "templates" / "landing.html").read_text()
        # Find the .hero-demo block — it's a 4-level nested structure
        # (demo → source + arrow + scenes → tile×5). Use a non-greedy
        # match that goes until the </div> after the scenes list.
        m = re.search(
            r'<div class="hero-demo"[^>]*aria-hidden="true">(.*?)<div class="hero-demo-tile"',
            src, re.DOTALL,
        )
        assert m, "could not find .hero-demo block"
        # After the first tile, count the rest
        rest = src[m.end():]
        tiles_after = re.findall(r'<div class="hero-demo-tile"', rest)
        # We already found 1, so we expect 4 more
        assert len(tiles_after) == 4, f"expected 4 more tiles after first, got {len(tiles_after)}"

    def test_tiles_have_data_scene_attributes(self):
        """Each tile needs data-scene="N" so the CSS can stagger the animation."""
        src = (REPO / "templates" / "landing.html").read_text()
        for n in range(1, 6):
            assert f'data-scene="{n}"' in src, f"missing data-scene={n}"

    def test_tiles_have_name_labels(self):
        src = (REPO / "templates" / "landing.html").read_text()
        # The 5 scene types the app supports
        for name in ("Studio", "In-hand", "Action", "Lifestyle", "With props"):
            assert name in src, f"missing scene name {name!r} in demo"

    def test_aria_hidden_on_decorative_block(self):
        """The demo is decorative — screen readers shouldn't read the
        animation. The block has aria-hidden=true so AT users skip it."""
        src = (REPO / "templates" / "landing.html").read_text()
        m = re.search(r'<div class="hero-demo"[^>]*aria-hidden="true"',
                      src)
        assert m, "missing aria-hidden=true on .hero-demo"

    def test_placeholder_product_image_present(self):
        """A CSS-drawn "product" rectangle (the .hero-demo-product div)
        is the visual source on the left of the demo."""
        src = (REPO / "templates" / "landing.html").read_text()
        assert 'class="hero-demo-product"' in src

    def test_source_label_present(self):
        """The 'Your product' label sits under the placeholder so the
        flow is clear: input on the left, scenes on the right."""
        src = (REPO / "templates" / "landing.html").read_text()
        assert "Your product" in src


class TestHeroDemoCSS:
    """The CSS keyframe + per-tile animation-delay must be present."""

    def test_keyframe_defined(self):
        css = (REPO / "static" / "app.css").read_text()
        assert "@keyframes hero-demo-tile" in css
        assert "@keyframes hero-demo-arrow-pulse" in css

    def test_per_tile_animation_delays(self):
        """Each tile must have its own animation-delay so the scene-set
        lights up one at a time. Without this, all 5 tiles would
        light up at once — the "demo" wouldn't demonstrate anything."""
        css = (REPO / "static" / "app.css").read_text()
        for n, delay in [(1, "0s"), (2, ".8s"), (3, "1.6s"),
                          (4, "2.4s"), (5, "3.2s")]:
            needle = f'.hero-demo-tile[data-scene="{n}"] {{ animation-delay: {delay};'
            assert needle in css, f"missing {needle!r}"

    def test_prefers_reduced_motion_respected(self):
        """Users with prefers-reduced-motion: reduce should NOT see the
        looping animation. This is an a11y requirement — some users
        get nauseous from looping motion. We rely on a GLOBAL
        prefers-reduced-motion block at the top of the file (line ~978)
        that sets all animation-duration to 0.01ms. This is the
        cleaner pattern than per-component overrides."""
        css = (REPO / "static" / "app.css").read_text()
        assert "@media (prefers-reduced-motion: reduce)" in css
        # The global block at the top covers * all elements
        # (the *, *::before, *::after selector). The demo inherits this.
        global_block = re.search(
            r'@media \(prefers-reduced-motion: reduce\)\s*\{\s*\*,\s*\*::before,\s*\*::after\s*\{[^}]+\}',
            css, re.DOTALL,
        )
        assert global_block, "no global prefers-reduced-motion block found"
        assert "animation-duration: 0.01ms" in global_block.group(0)
        # The .hero-demo-tile itself must use animation (so the global
        # block can stop it)
        assert ".hero-demo-tile" in css
        assert re.search(r"\.hero-demo-tile\s*\{[^}]*animation:", css, re.DOTALL), \
            ".hero-demo-tile doesn't have an animation property"

    def test_mobile_breakpoint_present(self):
        """Below 720px, the 5-tile row needs to stack. Without this,
        the tiles would be 1/5 of the viewport width and unreadable."""
        css = (REPO / "static" / "app.css").read_text()
        assert "@media (max-width: 720px)" in css
        # The flex-direction:column is what stacks the layout
        assert ".hero-demo { flex-direction: column" in css


class TestHeroDemoAccessibility:
    """The demo is decorative — no real content, no real actions. It
    shouldn't appear in the accessibility tree."""

    def test_no_form_or_button_in_demo(self):
        """A real form/button in the demo would imply an action. The
        demo is purely visual, so there should be no interactive
        elements inside .hero-demo."""
        src = (REPO / "templates" / "landing.html").read_text()
        m = re.search(r'<div class="hero-demo"[^>]*>.*?</div>\s*</div>\s*</div>',
                      src, re.DOTALL)
        assert m
        block = m.group(0)
        assert "<button" not in block, "demo block has a button — should be decorative only"
        assert "<form" not in block, "demo block has a form — should be decorative only"
        assert "<input" not in block, "demo block has an input — should be decorative only"
        assert "<a " not in block, "demo block has a link — should be decorative only"

    def test_no_images_or_svg_in_demo(self):
        """Pure CSS shapes (the product rectangle, the tile gradients).
        No asset loading required."""
        src = (REPO / "templates" / "landing.html").read_text()
        m = re.search(r'<div class="hero-demo"[^>]*>.*?</div>\s*</div>\s*</div>',
                      src, re.DOTALL)
        assert m
        block = m.group(0)
        assert "<img" not in block
        assert "<svg" not in block
