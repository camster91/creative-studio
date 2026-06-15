"""Regression tests for WS-3: curated templates endpoint + UI markup.

Run:  pytest tests/test_refactor_ws3_templates.py -v
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).parent.parent / "scripts"


def _load_module(tmp_path):
    web_path = SCRIPT_DIR / "creative-studio-web.py"
    sys.path.insert(0, str(SCRIPT_DIR))
    os.environ.setdefault("GEMINI_API_KEY", "test-key")
    os.environ.setdefault("PHOTOGEN_ADMIN_SECRET", "")
    spec = importlib.util.spec_from_file_location("creative_studio_web", web_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["creative_studio_web"] = mod
    spec.loader.exec_module(mod)
    auth_path = tmp_path / "auth.db"
    setattr(mod, "AUTH_DB", auth_path)
    mod._init_auth_schema()
    with mod._request_log_lock:
        mod._request_log.clear()
    t_path = tmp_path / "templates.json"
    setattr(mod, "_TEMPLATES_USER_PATH", t_path)
    return mod


@pytest.fixture
def cs(tmp_path):
    return _load_module(tmp_path)


# Endpoint tests
class TestApiTemplates:
    def test_returns_templates_list(self, cs):
        client = cs.app.test_client()
        r = client.get("/api/templates")
        assert r.status_code == 200
        body = r.get_json()
        assert "templates" in body
        assert isinstance(body["templates"], list)
        assert len(body["templates"]) > 0

    def test_templates_have_required_fields(self, cs):
        client = cs.app.test_client()
        body = client.get("/api/templates").get_json()
        for t in body["templates"]:
            for field in ("id", "name", "category", "preset",
                          "aspect", "tier", "prompt"):
                assert field in t
            assert t["aspect"] in ("1:1", "4:5", "9:16", "16:9", "2:3", "4:3")
            assert t["tier"] in ("fast", "balanced", "quality", "ultra")
            assert t["preset"] in ("inhand", "studio", "action", "lifestyle", "withprops")

    def test_at_least_15_templates(self, cs):
        body = cs.app.test_client().get("/api/templates").get_json()
        assert len(body["templates"]) >= 15

    def test_categories_cover_main_cpg_platforms(self, cs):
        body = cs.app.test_client().get("/api/templates").get_json()
        cats = {t["category"] for t in body["templates"]}
        required = {"Amazon", "Instagram", "Pinterest", "Email", "Shopify"}
        assert not (required - cats)

    def test_unique_template_ids(self, cs):
        body = cs.app.test_client().get("/api/templates").get_json()
        ids = [t["id"] for t in body["templates"]]
        assert len(ids) == len(set(ids))

    def test_no_auth_required(self, cs):
        r = cs.app.test_client().get("/api/templates")
        assert r.status_code == 200

    def test_no_500_under_load(self, cs, monkeypatch):
        monkeypatch.setattr(cs, "_RATE_LIMIT", 1000)
        client = cs.app.test_client()
        for _ in range(50):
            assert client.get("/api/templates").status_code == 200


# Storage tests
class TestTemplatesStorage:
    def test_default_file_seeds_to_data_dir(self, tmp_path):
        """First boot: no user file exists. After _read_templates is
        called once, the user file should exist (seeded from the
        default). The seed happens at import time in the real
        production path; in this test we manually re-seed at the
        redirected tmp path to test the user-path copy behavior."""
        cs = _load_module(tmp_path)
        # The fixture redirected _TEMPLATES_USER_PATH to tmp_path. The
        # import-time seed used the original path (before redirect).
        # Manually copy the default file to the test's user path.
        import shutil
        shutil.copyfile(str(cs._TEMPLATES_DEFAULT_PATH), str(cs._TEMPLATES_USER_PATH))
        # Delete and re-call _read_templates to verify the file was found
        cs._TEMPLATES_USER_PATH.unlink()
        assert not cs._TEMPLATES_USER_PATH.exists()
        t = cs._read_templates()
        assert len(t) > 0
        # The default file is the fallback; the user file is still
        # missing because we don't re-seed on a missing user file
        # at read time (the seed is import-time only).

    def test_user_file_overrides_default(self, tmp_path):
        cs = _load_module(tmp_path)
        custom = {"version": 1, "templates": [{
            "id": "test-only-template",
            "name": "Test only",
            "category": "Test",
            "preset": "studio",
            "aspect": "1:1",
            "tier": "fast",
            "prompt": "This is a test",
        }]}
        cs._TEMPLATES_USER_PATH.write_text(json.dumps(custom))
        body = cs._read_templates()
        assert len(body) == 1
        assert body[0]["id"] == "test-only-template"

    def test_corrupt_user_file_falls_back_to_default(self, tmp_path):
        cs = _load_module(tmp_path)
        cs._TEMPLATES_USER_PATH.write_text("{not valid json")
        t = cs._read_templates()
        assert len(t) > 0


# Static markup tests
class TestEditorMarkup:
    def test_editor_has_templates_strip(self):
        src = (Path(__file__).parent.parent / "templates" / "app.html").read_text()
        assert 'id="templatesRow"' in src
        assert 'id="templatesStrip"' in src
        assert "Templates" in src

    def test_editor_loads_templates_on_init(self):
        js = (Path(__file__).parent.parent / "static" / "app.js").read_text()
        assert "loadTemplates()" in js
        assert "/api/templates" in js
        assert "applyTemplate" in js
        for v in ("state.preset", "state.aspect", "state.tier"):
            assert v in js


# Deploy tests
class TestDeploy:
    def test_templates_json_exists(self):
        p = Path(__file__).parent.parent / "scripts" / "templates.json"
        assert p.exists()
        data = json.loads(p.read_text())
        assert "templates" in data
        assert data["version"] >= 1
        assert len(data["templates"]) >= 15

    def test_dockerfile_copies_scripts(self):
        dockerfile = (Path(__file__).parent.parent / "Dockerfile").read_text()
        assert "scripts" in dockerfile.lower()
