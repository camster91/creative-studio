"""
Creative Studio — Core backend flow tests (no API calls).
Tests cost tracking, session management, image URL resolution, and pin logic.

Run:  pytest tests/test_core.py -v
"""
import os
import sys
import tempfile
import importlib.util
from pathlib import Path

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

            cs.track_cost("gemini-3.1-flash-image-preview", 2)
            costs = cs.load_costs()
            assert costs["image_count"] == 2
            assert costs["total"] == pytest.approx(0.14, abs=0.001)


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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
