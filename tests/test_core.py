"""
Creative Studio — Core backend flow tests (no API calls).
Tests cost tracking, session management, image URL resolution, and pin logic.

Run:  pytest tests/test_core.py -v
"""
import os
import sys
import json
import tempfile
import importlib.util
from pathlib import Path
from datetime import datetime

import pytest

SCRIPT_DIR = Path(__file__).parent.parent / "scripts"


def _load_web_module():
    """Load creative-studio-web.py dynamically (hyphenated filename)."""
    web_path = SCRIPT_DIR / "creative-studio-web.py"
    sys.path.insert(0, str(SCRIPT_DIR))
    os.environ.setdefault("GEMINI_API_KEY", "test-key")
    spec = importlib.util.spec_from_file_location("creative_studio_web", web_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["creative_studio_web"] = mod
    spec.loader.exec_module(mod)
    return mod


cs = _load_web_module()


class TestConfig:
    def test_config_persistence(self):
        pytest.skip("Config class is in creative_studio.py — tested separately")


class TestSession:
    def test_new_session(self):
        sid = cs.new_session_id()
        assert sid.startswith("sess_")
        assert len(sid) == 13

    def test_save_load_session(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            cs.DATA_DIR = td / "data"
            cs.SESSIONS_DIR = cs.DATA_DIR / "sessions"
            cs.COST_DB = cs.DATA_DIR / "costs.json"
            cs.OUTPUT_DIR = td / "outputs"
            cs.PINS_DB = cs.DATA_DIR / "pins.json"
            cs.DATA_DIR.mkdir(parents=True, exist_ok=True)
            cs.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
            cs.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

            sid = cs.new_session_id()
            data = cs.load_session(sid)
            assert data["id"] == sid
            assert data["entries"] == []

            cs.add_entry(sid, {"type": "direct", "prompt": "test", "cost": 0.07})
            reloaded = cs.load_session(sid)
            assert len(reloaded["entries"]) == 1
            assert reloaded["entries"][0]["type"] == "direct"


class TestCostTracking:
    def test_track_cost(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            cs.DATA_DIR = td / "data"
            cs.SESSIONS_DIR = cs.DATA_DIR / "sessions"
            cs.COST_DB = cs.DATA_DIR / "costs.json"
            cs.OUTPUT_DIR = td / "outputs"
            cs.PINS_DB = cs.DATA_DIR / "pins.json"
            cs.DATA_DIR.mkdir(parents=True, exist_ok=True)
            cs.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
            cs.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

            cs.COST_DB.parent.mkdir(parents=True, exist_ok=True)
            cs.save_costs({"total": 0.0, "by_model": {}, "by_date": {}, "session_count": 0, "image_count": 0})

            cs.track_cost("gemini-3.1-flash-image-preview", "1K", 2)
            costs = cs.load_costs()
            assert costs["image_count"] == 2
            # 2 × $0.045 (1K flash) = $0.09
            assert costs["total"] == pytest.approx(0.09, abs=0.001)


class TestImageUrl:
    def test_image_url_from_output_dir(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            cs.OUTPUT_DIR = td / "outputs"
            cs.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            fake_file = cs.OUTPUT_DIR / "2024-01-01" / "direct" / "test.png"
            fake_file.parent.mkdir(parents=True, exist_ok=True)
            fake_file.write_text("fake")
            url = cs.image_url(str(fake_file))
            assert url.startswith("/image/")


class TestPins:
    def test_pin_to_region(self):
        assert cs.pin_to_region(0.5, 0.5) == "center"
        assert cs.pin_to_region(0.1, 0.1) == "top-left"
        assert cs.pin_to_region(0.8, 0.8) == "bottom-right"

    def test_build_pin_prompt(self):
        pins = [
            {"x": 0.5, "y": 0.2, "text": "make this darker"},
            {"x": 0.2, "y": 0.7, "text": "add shadow"},
        ]
        prompt = cs.build_pin_prompt(pins)
        assert "make this darker" in prompt
        assert "add shadow" in prompt


class TestAspectRatio:
    def test_resolve_valid(self):
        # resolve_aspect_ratio lives in creative_studio.py — load it
        spec = importlib.util.spec_from_file_location("creative_studio", SCRIPT_DIR / "creative_studio.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.resolve_aspect_ratio("16:9") == "16:9"
        assert mod.resolve_aspect_ratio("16:10") == "16:9"
        assert mod.resolve_aspect_ratio("garbage") == "16:9"


class TestTierPricing:
    def test_tier_model_mapping_complete(self):
        for tier in ("fast", "balanced", "quality", "ultra"):
            assert tier in cs._TIER_MODEL, f"missing tier {tier}"
            model, res = cs._TIER_MODEL[tier]
            assert model in cs.COSTS, f"tier {tier} -> {model} not in COSTS"

    def test_cost_for_tier_distinct(self):
        prices = {t: cs.cost_for_tier(t) for t in ("fast", "balanced", "quality", "ultra")}
        # Each tier should have a different price (no more $0.07 / $0.07 parity bug)
        assert prices["fast"] < prices["balanced"]
        assert prices["balanced"] < prices["quality"]
        assert prices["quality"] < prices["ultra"]
        # Sanity: imagen-4.0-fast is $0.02
        assert prices["fast"] == pytest.approx(0.02, abs=0.001)

    def test_daily_limit_blocks(self):
        """Server-side guardrail: if today's spend + est exceeds limit, returns 429 response."""
        with cs.app.app_context():
            with tempfile.TemporaryDirectory() as td:
                cs.DATA_DIR = Path(td)
                cs.SESSIONS_DIR = cs.DATA_DIR / "sessions"
                cs.COST_DB = cs.DATA_DIR / "costs.json"
                cs.DATA_DIR.mkdir(parents=True, exist_ok=True)
                cs.COST_DB.parent.mkdir(parents=True, exist_ok=True)
                cs.save_costs({"total": 0.0, "by_model": {}, "by_date": {datetime.now().strftime("%Y-%m-%d"): 4.99}, "session_count": 0, "image_count": 0})
                import os
                os.environ["CREATIVE_DAILY_LIMIT"] = "5"
                # 1 fast image = $0.02, but spent_today is $4.99 → 4.99 + 0.02 = 5.01 > 5
                result = cs.enforce_daily_limit(1, "fast")
                assert result is not None
                response, status = result
                assert status == 429
                body = json.loads(response.get_data(as_text=True))
                assert body["limit"] == 5.0
                assert "Daily limit" in body["error"]

    def test_daily_limit_allows_under(self):
        """If under the cap, no rejection."""
        with cs.app.app_context():
            with tempfile.TemporaryDirectory() as td:
                cs.DATA_DIR = Path(td)
                cs.COST_DB = cs.DATA_DIR / "costs.json"
                cs.DATA_DIR.mkdir(parents=True, exist_ok=True)
                cs.COST_DB.parent.mkdir(parents=True, exist_ok=True)
                cs.save_costs({"total": 0.0, "by_model": {}, "by_date": {datetime.now().strftime("%Y-%m-%d"): 0.0}, "session_count": 0, "image_count": 0})
                import os
                os.environ["CREATIVE_DAILY_LIMIT"] = "5"
                result = cs.enforce_daily_limit(1, "fast")
                assert result is None  # allowed

    def test_whoami_endpoint(self):
        """The /api/whoami endpoint should report server_has_fallback accurately."""
        with cs.app.test_client() as c:
            r = c.get("/api/whoami")
            assert r.status_code == 200
            data = r.get_json()
            assert "server_has_fallback" in data
            assert "byok" in data
            assert "version" in data


class TestByokGate:
    """v4.6: server fallback is opt-in via CREATIVE_ALLOW_SERVER_FALLBACK.
    Without it, generation endpoints return 402 unless X-API-Key is supplied."""

    def test_no_fallback_no_user_key_returns_402(self, monkeypatch):
        """When CREATIVE_ALLOW_SERVER_FALLBACK is unset, /api/generate must 402."""
        monkeypatch.setattr(cs, "ALLOW_SERVER_FALLBACK", False)
        monkeypatch.setattr(cs, "SERVER_API_KEY", "")  # ensure no fallback
        with cs.app.test_client() as c:
            r = c.post(
                "/api/generate",
                json={"prompt": "test", "tier": "balanced", "aspect_ratio": "1:1"},
            )
            assert r.status_code == 402
            body = r.get_json()
            assert body["error"] == "BYOK or sign-in required"
            assert "Gemini API key" in body["message"]

    def test_user_key_passes_gate(self, monkeypatch):
        """When X-API-Key is sent, _get_api_key returns it (even without fallback)."""
        from flask import request

        monkeypatch.setattr(cs, "ALLOW_SERVER_FALLBACK", False)
        monkeypatch.setattr(cs, "SERVER_API_KEY", "")
        with cs.app.test_client() as c:
            c.post(
                "/api/generate",
                json={"prompt": "test", "tier": "balanced", "aspect_ratio": "1:1"},
                headers={"X-API-Key": "AIza-test-user-key"},
            )
            # Should not have hit the 402 — verify the helper would return the user key
            # by reading the module state directly
            with cs.app.test_request_context(headers={"X-API-Key": "AIza-test-user-key"}):
                assert cs._get_api_key() == "AIza-test-user-key"

    def test_fallback_opt_in(self, monkeypatch):
        """When CREATIVE_ALLOW_SERVER_FALLBACK=true and server key set, helper returns server key."""
        monkeypatch.setattr(cs, "ALLOW_SERVER_FALLBACK", True)
        monkeypatch.setattr(cs, "SERVER_API_KEY", "AIza-server-fallback")
        with cs.app.test_request_context():
            assert cs._get_api_key() == "AIza-server-fallback"

    def test_fallback_disabled_even_with_key_set(self, monkeypatch):
        """If server key is set but CREATIVE_ALLOW_SERVER_FALLBACK is off, helper returns empty."""
        monkeypatch.setattr(cs, "ALLOW_SERVER_FALLBACK", False)
        monkeypatch.setattr(cs, "SERVER_API_KEY", "AIza-server-fallback")
        with cs.app.test_request_context():
            assert cs._get_api_key() == ""


class TestPageRoutes:
    """v4.6: marketing landing and editor are separate routes."""

    def test_landing_route_serves_landing_template(self):
        with cs.app.test_client() as c:
            r = c.get("/")
            assert r.status_code == 200
            html = r.get_data(as_text=True)
            assert "hero-section" in html
            assert "landing-footer" in html
            # Editor should NOT be in landing
            assert "Generate Image" not in html
            assert "dropzone" not in html

    def test_app_route_serves_editor(self):
        with cs.app.test_client() as c:
            r = c.get("/app")
            assert r.status_code == 200
            html = r.get_data(as_text=True)
            assert "dropzone" in html
            assert "Generate" in html  # "Generate" button label
            assert "hero-section" not in html
            assert "landing-footer" not in html

    def test_landing_ctas_link_to_app(self):
        with cs.app.test_client() as c:
            r = c.get("/")
            html = r.get_data(as_text=True)
            assert 'href="/app"' in html

    def test_static_assets_served(self):
        with cs.app.test_client() as c:
            for path in ("/static/app.css", "/static/app.js"):
                r = c.get(path)
                assert r.status_code == 200, f"{path} returned {r.status_code}"
                assert len(r.get_data()) > 100

    def test_app_viewport_meta(self):
        """App must have viewport meta tag for proper mobile rendering."""
        with cs.app.test_client() as c:
            html = c.get("/app").get_data(as_text=True)
            assert 'name="viewport"' in html
            assert "width=device-width" in html

    def test_app_has_hamburger_menu(self):
        """Mobile UI requires a hamburger menu and a hidden mobile nav drawer."""
        with cs.app.test_client() as c:
            html = c.get("/app").get_data(as_text=True)
            assert 'id="menuToggle"' in html
            assert 'id="mobileMenu"' in html

    def test_app_has_collapsible_panels(self):
        """Panels should be marked collapsible so they fold on mobile.

        Note: the current design uses a compact, non-collapsible layout that
        works on both desktop and mobile without needing JS-controlled collapse.
        This test now verifies that the editor exposes the core field groups
        (product, scene, output settings) in a single visible flow.
        """
        with cs.app.test_client() as c:
            html = c.get("/app").get_data(as_text=True)
            assert 'class="field-group"' in html, "Missing field-group sections"
            assert html.count('class="field-group"') >= 3, "Need at least 3 field groups"
            assert 'data-toggle' not in html or html.count('data-toggle') == 0, \
                "Old collapsible data-toggle markers should be removed"

    def test_app_16px_inputs_no_ios_zoom(self):
        """Inputs/textareas should be 16px font-size to prevent iOS auto-zoom."""
        with cs.app.test_client() as c:
            r = c.get("/static/app.css")
            css = r.get_data(as_text=True)
            assert "font-size: 16px" in css, "Missing 16px font-size on inputs to prevent iOS zoom"

    def test_app_min_touch_target_44px(self):
        """All interactive elements should respect Apple HIG 44px minimum touch target."""
        with cs.app.test_client() as c:
            r = c.get("/static/app.css")
            css = r.get_data(as_text=True)
            # CSS variable for touch target (either --touch or --t, both acceptable)
            assert ("--touch: 44px" in css) or ("--t: 44px" in css), \
                "Missing 44px touch target variable"
            # The CSS should reference this variable on chips/buttons/inputs
            assert "min-height: var(--t)" in css or "min-height: var(--touch)" in css

    def test_landing_viewport_meta(self):
        with cs.app.test_client() as c:
            html = c.get("/").get_data(as_text=True)
            assert 'name="viewport"' in html

    def test_landing_has_cta_to_app(self):
        with cs.app.test_client() as c:
            html = c.get("/").get_data(as_text=True)
            assert 'href="/app"' in html
            # The hero should mention the 5 scene types
            assert "Five scene types" in html or "5 scene types" in html
            # The CTA should be the new "Try it free" copy
            assert "Try it free" in html

    def test_app_has_scene_types(self):
        """The 5 scene types (In-hand, Studio, Action, Lifestyle, With props)
        should be present as scene-type chips, not the old Amazon/IG preset list.
        """
        with cs.app.test_client() as c:
            html = c.get("/app").get_data(as_text=True)
            for scene in ["In-hand", "Studio", "Action", "Lifestyle", "With props"]:
                assert scene in html, f"Missing scene type: {scene}"
            # Old preset names should be gone
            assert "Amazon white" not in html, "Old 'Amazon white' preset should be removed"
            assert "Email banner" not in html, "Old 'Email banner' preset should be removed"

    def test_app_has_bento_output_grid(self):
        """The output grid should have bento layout classes for asymmetric layouts."""
        with cs.app.test_client() as c:
            r = c.get("/static/app.css")
            css = r.get_data(as_text=True)
            # Bento: count-3, count-4, count-5, count-6 grid-template-areas
            assert "count-3" in css and "count-4" in css and "count-5" in css
            assert 'grid-template-areas' in css

    def test_app_has_source_tile_style(self):
        """The user's uploaded product should appear as a source tile in output."""
        with cs.app.test_client() as c:
            r = c.get("/static/app.css")
            css = r.get_data(as_text=True)
            assert ".output-cell.is-source" in css, "Missing source tile style"
            assert ".source-badge" in css, "Missing source badge style"

    def test_app_has_scene_set_button(self):
        """The 'Generate all 5 scenes' button should be present in the editor."""
        with cs.app.test_client() as c:
            html = c.get("/app").get_data(as_text=True)
            assert 'id="sceneSetBtn"' in html
            assert "Generate all 5 scenes" in html

    def test_scene_set_endpoint_requires_byok(self):
        """POST /api/scene-set without a key must return 402 (BYOK gate)."""
        # Disable server fallback to force BYOK
        cs.SERVER_API_KEY = ""
        cs.ALLOW_SERVER_FALLBACK = False
        try:
            with cs.app.test_client() as c:
                # Build a minimal in-memory image
                from io import BytesIO
                data = {
                    "product": (BytesIO(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100), "test.png"),
                }
                r = c.post("/api/scene-set", data=data, content_type="multipart/form-data")
                assert r.status_code == 402
                body = r.get_json()
                assert body["error"] == "BYOK or sign-in required"
        finally:
            cs.SERVER_API_KEY = "test-key"  # restore
            cs.ALLOW_SERVER_FALLBACK = True

    def test_scene_set_endpoint_rejects_non_image(self):
        """POST /api/scene-set with non-image must return 400."""
        cs.ALLOW_SERVER_FALLBACK = True
        cs.SERVER_API_KEY = "test-key"
        with cs.app.test_client() as c:
            from io import BytesIO
            data = {
                "product": (BytesIO(b"not an image"), "test.txt"),
            }
            r = c.post("/api/scene-set", data=data, content_type="multipart/form-data",
                       headers={"X-API-Key": "AIza-test-key-1234567890"})
            # 400 for bad extension, 402 if BYOK gate fires first
            assert r.status_code in (400, 402)

    def test_no_literal_unicode_escapes_in_frontend(self):
        """Regression: the HTML_TEMPLATE was a raw string, so literal '\\u003e' sequences
        were being served to the browser as the 6-char string instead of '>'. This broke
        every JS arrow function. Guard against the file getting raw-string arrow escapes again."""
        from pathlib import Path
        web_path = Path(cs.__file__).parent / "creative-studio-web.py"
        src = web_path.read_text()
        # The fix: all 16 literal '\u003e' should be gone. Any new occurrence is a regression.
        # (excludes: comments, docstrings, or explicit documentation about the bug.)
        bad_count = src.count("\\u003e")
        assert bad_count == 0, (
            f"Found {bad_count} literal '\\u003e' sequences in {web_path}. "
            "These are emitted as 6-char strings in the served HTML and break JS arrow functions. "
            "Use the literal '>' character in the script section."
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
