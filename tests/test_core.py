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
