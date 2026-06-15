"""
Regression tests for WS-7: asset library (the "every generation
the user has ever made" view).

Two new endpoints:
  GET  /api/library             — paginated list of every generation
  POST /api/library/<path>/delete — delete one file

Both require a signed-in session. Anonymous gets 401.

The library scans OUTPUT_DIR for *.png / *.jpg. Each entry
includes the parsed aspect ratio (best-effort from filename),
mtime, size, and the prompt text from the sidecar JSON if
present.

Run:  pytest tests/test_refactor_ws7_library.py -v
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
    spec = importlib.util.spec_from_file_location("creative_studio_web", web_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["creative_studio_web"] = mod
    spec.loader.exec_module(mod)
    auth_path = tmp_path / "auth.db"
    setattr(mod, "AUTH_DB", auth_path)
    mod._init_auth_schema()
    # Redirect OUTPUT_DIR to a per-test tmp dir
    out_dir = tmp_path / "outputs"
    out_dir.mkdir()
    setattr(mod, "OUTPUT_DIR", out_dir)
    with mod._request_log_lock:
        mod._request_log.clear()
    return mod


@pytest.fixture
def cs(tmp_path):
    return _load_module(tmp_path)


def _signup_and_login(client, email):
    r = client.post("/signup", json={"email": email})
    token = r.get_json()["token"]
    r = client.post("/login", json={"token": token})
    return r.get_json()["session_token"]


def _make_image(out_dir, date_str, name, prompt="a sample prompt", aspect="1:1"):
    """Helper: write a fake image + sidecar JSON in OUTPUT_DIR so
    the library scanner picks it up."""
    date_dir = out_dir / date_str
    date_dir.mkdir(parents=True, exist_ok=True)
    img = date_dir / name
    img.write_bytes(b"fake-png")
    # Sidecar JSON with the prompt
    sidecar = img.with_suffix(".json")
    sidecar.write_text(json.dumps({"prompt": prompt, "model": "test"}))
    return img, sidecar


# ─── /api/library auth gate ───────────────────────────────────────

class TestLibraryAuth:
    def test_anonymous_returns_401(self, cs):
        r = cs.app.test_client().get("/api/library")
        assert r.status_code == 401

    def test_signed_in_returns_200(self, cs):
        client = cs.app.test_client()
        sess = _signup_and_login(client, "alice@x.co")
        r = client.get("/api/library", headers={"X-Session-Token": sess})
        assert r.status_code == 200


# ─── /api/library list shape ──────────────────────────────────────

class TestLibraryList:
    def test_empty_returns_no_items(self, cs):
        client = cs.app.test_client()
        sess = _signup_and_login(client, "alice@x.co")
        body = client.get("/api/library",
            headers={"X-Session-Token": sess}).get_json()
        assert body["items"] == []
        assert body["total"] == 0

    def test_returns_images_with_metadata(self, cs):
        out_dir = cs.OUTPUT_DIR
        _make_image(out_dir, "2026-06-15", "test_1_1.png",
                    prompt="a coffee bag", aspect="1:1")
        client = cs.app.test_client()
        sess = _signup_and_login(client, "alice@x.co")
        body = client.get("/api/library",
            headers={"X-Session-Token": sess}).get_json()
        assert body["total"] == 1
        item = body["items"][0]
        assert item["name"] == "test_1_1.png"
        assert item["url"] == "/image/2026-06-15/test_1_1.png"
        assert item["aspect"] == "1:1"
        assert "a coffee bag" in item["prompt"]
        assert item["size"] > 0
        assert item["mtime"] > 0

    def test_aspect_parsed_from_filename(self, cs):
        out_dir = cs.OUTPUT_DIR
        _make_image(out_dir, "2026-06-15", "thing_4_5.png", aspect="4:5")
        _make_image(out_dir, "2026-06-15", "thing_9_16.png", aspect="9:16")
        _make_image(out_dir, "2026-06-15", "thing_16_9.png", aspect="16:9")
        client = cs.app.test_client()
        sess = _signup_and_login(client, "alice@x.co")
        body = client.get("/api/library",
            headers={"X-Session-Token": sess}).get_json()
        aspects = {item["aspect"] for item in body["items"]}
        assert aspects == {"4:5", "9:16", "16:9"}

    def test_pagination(self, cs):
        out_dir = cs.OUTPUT_DIR
        for i in range(10):
            _make_image(out_dir, "2026-06-15", f"img_{i:02d}_1_1.png")
        client = cs.app.test_client()
        sess = _signup_and_login(client, "alice@x.co")
        body = client.get("/api/library?limit=3&offset=0",
            headers={"X-Session-Token": sess}).get_json()
        assert body["total"] == 10
        assert len(body["items"]) == 3
        assert body["limit"] == 3
        assert body["offset"] == 0
        # Next page
        body2 = client.get("/api/library?limit=3&offset=3",
            headers={"X-Session-Token": sess}).get_json()
        assert len(body2["items"]) == 3
        # Different items
        assert body["items"][0]["name"] != body2["items"][0]["name"]

    def test_max_limit_is_200(self, cs):
        out_dir = cs.OUTPUT_DIR
        for i in range(3):
            _make_image(out_dir, "2026-06-15", f"img_{i}_1_1.png")
        client = cs.app.test_client()
        sess = _signup_and_login(client, "alice@x.co")
        body = client.get("/api/library?limit=9999",
            headers={"X-Session-Token": sess}).get_json()
        assert body["limit"] == 200

    def test_limit_capped_at_200_not_more(self, cs):
        out_dir = cs.OUTPUT_DIR
        # 250 items
        for i in range(250):
            _make_image(out_dir, "2026-06-15", f"img_{i:03d}_1_1.png")
        client = cs.app.test_client()
        sess = _signup_and_login(client, "alice@x.co")
        body = client.get("/api/library?limit=9999",
            headers={"X-Session-Token": sess}).get_json()
        assert body["total"] == 250
        assert len(body["items"]) == 200  # capped

    def test_invalid_limit_defaults_to_60(self, cs):
        out_dir = cs.OUTPUT_DIR
        for i in range(3):
            _make_image(out_dir, "2026-06-15", f"img_{i}_1_1.png")
        client = cs.app.test_client()
        sess = _signup_and_login(client, "alice@x.co")
        body = client.get("/api/library?limit=invalid",
            headers={"X-Session-Token": sess}).get_json()
        assert body["limit"] == 60


# ─── Filtering ────────────────────────────────────────────────────

class TestLibraryFilter:
    def test_filter_by_aspect(self, cs):
        out_dir = cs.OUTPUT_DIR
        _make_image(out_dir, "2026-06-15", "a_1_1.png", aspect="1:1")
        _make_image(out_dir, "2026-06-15", "b_4_5.png", aspect="4:5")
        _make_image(out_dir, "2026-06-15", "c_4_5.png", aspect="4:5")
        client = cs.app.test_client()
        sess = _signup_and_login(client, "alice@x.co")
        body = client.get("/api/library?aspect=4:5",
            headers={"X-Session-Token": sess}).get_json()
        assert body["total"] == 2
        for item in body["items"]:
            assert item["aspect"] == "4:5"

    def test_filter_by_search_substring(self, cs):
        out_dir = cs.OUTPUT_DIR
        _make_image(out_dir, "2026-06-15", "a.png", prompt="a coffee bag on a table")
        _make_image(out_dir, "2026-06-15", "b.png", prompt="a tea mug on a counter")
        _make_image(out_dir, "2026-06-15", "c.png", prompt="a coffee grinder")
        client = cs.app.test_client()
        sess = _signup_and_login(client, "alice@x.co")
        body = client.get("/api/library?search=coffee",
            headers={"X-Session-Token": sess}).get_json()
        assert body["total"] == 2
        for item in body["items"]:
            assert "coffee" in item["prompt"].lower()

    def test_search_is_case_insensitive(self, cs):
        out_dir = cs.OUTPUT_DIR
        _make_image(out_dir, "2026-06-15", "a.png", prompt="COFFEE bag")
        client = cs.app.test_client()
        sess = _signup_and_login(client, "alice@x.co")
        body = client.get("/api/library?search=coffee",
            headers={"X-Session-Token": sess}).get_json()
        assert body["total"] == 1

    def test_no_match_returns_empty(self, cs):
        out_dir = cs.OUTPUT_DIR
        _make_image(out_dir, "2026-06-15", "a.png", prompt="a coffee bag")
        client = cs.app.test_client()
        sess = _signup_and_login(client, "alice@x.co")
        body = client.get("/api/library?search=xyzzy",
            headers={"X-Session-Token": sess}).get_json()
        assert body["items"] == []
        assert body["total"] == 0

    def test_aspect_and_search_combine(self, cs):
        out_dir = cs.OUTPUT_DIR
        _make_image(out_dir, "2026-06-15", "match_4_5.png",
                    prompt="coffee bag", aspect="4:5")
        _make_image(out_dir, "2026-06-15", "other_4_5.png",
                    prompt="tea bag", aspect="4:5")
        _make_image(out_dir, "2026-06-15", "match_1_1.png",
                    prompt="coffee grinder", aspect="1:1")
        client = cs.app.test_client()
        sess = _signup_and_login(client, "alice@x.co")
        body = client.get("/api/library?aspect=4:5&search=coffee",
            headers={"X-Session-Token": sess}).get_json()
        assert body["total"] == 1
        assert body["items"][0]["name"] == "match_4_5.png"


# ─── Sidecar / prompt loading ─────────────────────────────────────

class TestSidecarLoading:
    def test_no_sidecar_means_empty_prompt(self, cs):
        out_dir = cs.OUTPUT_DIR
        date = out_dir / "2026-06-15"
        date.mkdir(parents=True)
        (date / "no_sidecar_1_1.png").write_bytes(b"x")
        client = cs.app.test_client()
        sess = _signup_and_login(client, "alice@x.co")
        body = client.get("/api/library",
            headers={"X-Session-Token": sess}).get_json()
        assert body["items"][0]["prompt"] == ""

    def test_corrupt_sidecar_returns_empty_prompt(self, cs):
        out_dir = cs.OUTPUT_DIR
        date = out_dir / "2026-06-15"
        date.mkdir(parents=True)
        (date / "bad_1_1.png").write_bytes(b"x")
        (date / "bad_1_1.json").write_text("not valid json")
        client = cs.app.test_client()
        sess = _signup_and_login(client, "alice@x.co")
        body = client.get("/api/library",
            headers={"X-Session-Token": sess}).get_json()
        # Should not crash
        assert body["items"][0]["prompt"] == ""

    def test_sidecar_with_different_key(self, cs):
        out_dir = cs.OUTPUT_DIR
        date = out_dir / "2026-06-15"
        date.mkdir(parents=True)
        (date / "x_1_1.png").write_bytes(b"x")
        # The loader tries prompt, user_prompt, original_prompt
        (date / "x_1_1.json").write_text(json.dumps({"user_prompt": "the user wrote this"}))
        client = cs.app.test_client()
        sess = _signup_and_login(client, "alice@x.co")
        body = client.get("/api/library",
            headers={"X-Session-Token": sess}).get_json()
        assert body["items"][0]["prompt"] == "the user wrote this"


# ─── /api/library/<path>/delete ──────────────────────────────────

class TestLibraryDelete:
    def test_anonymous_returns_401(self, cs):
        r = cs.app.test_client().post("/api/library/whatever.png/delete")
        assert r.status_code == 401

    def test_delete_existing_file(self, cs):
        out_dir = cs.OUTPUT_DIR
        date = out_dir / "2026-06-15"
        date.mkdir(parents=True)
        img = date / "doomed_1_1.png"
        img.write_bytes(b"x")
        sidecar = img.with_suffix(".json")
        sidecar.write_text('{"prompt": "x"}')
        client = cs.app.test_client()
        sess = _signup_and_login(client, "alice@x.co")
        r = client.post(f"/api/library/{img.relative_to(out_dir)}/delete",
            headers={"X-Session-Token": sess})
        assert r.status_code == 200
        assert r.get_json()["deleted"] is True
        # Both file and sidecar are gone
        assert not img.is_file()
        assert not sidecar.is_file()

    def test_delete_nonexistent_returns_404(self, cs):
        """The path must survive the path-validity check (date dir + .png)
        AND the file must not exist. We use a valid path that doesn't exist."""
        client = cs.app.test_client()
        sess = _signup_and_login(client, "alice@x.co")
        r = client.post("/api/library/2026-06-15/missing_1_1.png/delete",
            headers={"X-Session-Token": sess})
        assert r.status_code == 404

    def test_delete_with_path_traversal_blocked(self, cs):
        """A request like /api/library/../etc/passwd/delete should
        be 400, not silently delete a file outside OUTPUT_DIR."""
        client = cs.app.test_client()
        sess = _signup_and_login(client, "alice@x.co")
        r = client.post("/api/library/../etc/passwd/delete",
            headers={"X-Session-Token": sess})
        # Either 400 (invalid path) or 404 (file not found)
        # Both are acceptable; 500 would be a bug.
        assert r.status_code in (400, 404)


# ─── Editor markup ────────────────────────────────────────────────

class TestLibraryEditorMarkup:
    def test_editor_has_library_section(self):
        from pathlib import Path
        src = (Path(__file__).parent.parent / "templates" / "app.html").read_text()
        assert 'id="libraryPanel"' in src
        assert 'id="libraryGrid"' in src
        assert 'id="librarySearch"' in src
        assert 'id="libraryAspectFilter"' in src

    def test_editor_has_more_like_this_button_html(self):
        """The More like this button is in buildCellHTML; it's
        rendered dynamically so the test checks the JS."""
        from pathlib import Path
        js = (Path(__file__).parent.parent / "static" / "app.js").read_text()
        assert 'more-like-this' in js
        assert 'data-prompt=' in js

    def test_editor_calls_loadLibrary_on_boot(self):
        from pathlib import Path
        js = (Path(__file__).parent.parent / "static" / "app.js").read_text()
        assert "loadLibrary()" in js
        assert "/api/library" in js
