"""
Regression tests for WS-4: projects (user-owned workspaces with
generations, exported as a zip). The endpoints are:

  POST   /api/projects                       — create
  GET    /api/projects                       — list (no generations)
  GET    /api/projects/<id>                  — full project
  DELETE /api/projects/<id>                  — delete
  POST   /api/projects/<id>/generations      — add a generation
  GET    /api/projects/<id>/export           — download as zip

All require a signed-in session. Anonymous requests get 401.
A user can only see their own projects (cross-user access returns 404,
not 403, to avoid leaking existence).

Run:  pytest tests/test_refactor_ws4_projects.py -v
"""
import importlib.util
import io
import json
import os
import sys
import zipfile
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
    return mod


@pytest.fixture
def cs(tmp_path):
    return _load_module(tmp_path)


def _signup_and_login(client, email):
    r = client.post("/signup", json={"email": email})
    token = r.get_json()["token"]
    r = client.post("/login", json={"token": token})
    return r.get_json()["session_token"]


# ─── Auth gate ───────────────────────────────────────────────────────

class TestAuth:
    def test_anonymous_create_returns_401(self, cs):
        r = cs.app.test_client().post("/api/projects", json={"name": "X"})
        assert r.status_code == 401

    def test_anonymous_list_returns_401(self, cs):
        r = cs.app.test_client().get("/api/projects")
        assert r.status_code == 401

    def test_anonymous_export_returns_401(self, cs):
        r = cs.app.test_client().get("/api/projects/whatever/export")
        assert r.status_code == 401


# ─── CRUD ────────────────────────────────────────────────────────────

class TestProjectCRUD:
    def test_create_returns_201_with_empty_generations(self, cs):
        client = cs.app.test_client()
        sess = _signup_and_login(client, "creator@x.co")
        r = client.post("/api/projects",
            json={"name": "My launch"},
            headers={"X-Session-Token": sess})
        assert r.status_code == 201
        body = r.get_json()
        assert body["name"] == "My launch"
        assert body["generations"] == []
        assert body["hero_url"] is None
        assert "id" in body

    def test_create_default_name(self, cs):
        client = cs.app.test_client()
        sess = _signup_and_login(client, "creator2@x.co")
        r = client.post("/api/projects", json={},
            headers={"X-Session-Token": sess})
        assert r.status_code == 201
        assert r.get_json()["name"] == "Untitled project"

    def test_list_returns_user_projects(self, cs):
        """The list endpoint returns the user's projects. The order
        is by updated_at DESC with id DESC as the tiebreaker. For
        projects created in the same second, the order is whatever
        the DB returns — we just check the SET of projects here.
        (The 'C is newest' assertion is a soft check; with
        millisecond-identical inserts it's racy.)"""
        import time
        client = cs.app.test_client()
        sess = _signup_and_login(client, "lister@x.co")
        for name in ("A", "B", "C"):
            client.post("/api/projects", json={"name": name},
                headers={"X-Session-Token": sess})
            time.sleep(1.05)  # ensure unique updated_at seconds
        r = client.get("/api/projects", headers={"X-Session-Token": sess})
        body = r.get_json()
        names = {p["name"] for p in body["projects"]}
        assert names == {"A", "B", "C"}
        # C was inserted last → newest updated_at → first in DESC
        assert body["projects"][0]["name"] == "C"

    def test_get_returns_full_project(self, cs):
        client = cs.app.test_client()
        sess = _signup_and_login(client, "getter@x.co")
        r = client.post("/api/projects", json={"name": "P"},
            headers={"X-Session-Token": sess})
        pid = r.get_json()["id"]
        r = client.get(f"/api/projects/{pid}",
            headers={"X-Session-Token": sess})
        assert r.status_code == 200
        body = r.get_json()
        assert body["id"] == pid
        assert body["name"] == "P"

    def test_delete_removes_project(self, cs):
        client = cs.app.test_client()
        sess = _signup_and_login(client, "deleter@x.co")
        r = client.post("/api/projects", json={"name": "P"},
            headers={"X-Session-Token": sess})
        pid = r.get_json()["id"]
        r = client.delete(f"/api/projects/{pid}",
            headers={"X-Session-Token": sess})
        assert r.status_code == 200
        # Confirm it's gone
        r = client.get(f"/api/projects/{pid}",
            headers={"X-Session-Token": sess})
        assert r.status_code == 404

    def test_cross_user_access_returns_404(self, cs):
        """User A creates a project, User B tries to read it. B
        should get 404 (not 403, to avoid leaking existence)."""
        client = cs.app.test_client()
        sess_a = _signup_and_login(client, "alpha@x.co")
        sess_b = _signup_and_login(client, "bravo@x.co")
        r = client.post("/api/projects", json={"name": "A's project"},
            headers={"X-Session-Token": sess_a})
        pid = r.get_json()["id"]
        r = client.get(f"/api/projects/{pid}",
            headers={"X-Session-Token": sess_b})
        assert r.status_code == 404


# ─── Generations ─────────────────────────────────────────────────────

class TestGenerations:
    def test_add_generation_appends_to_list(self, cs):
        client = cs.app.test_client()
        sess = _signup_and_login(client, "adder@x.co")
        r = client.post("/api/projects", json={"name": "P"},
            headers={"X-Session-Token": sess})
        pid = r.get_json()["id"]
        # Add 2 generations
        for i in range(2):
            r = client.post(f"/api/projects/{pid}/generations",
                json={"url": f"/image/test-{i}.png", "prompt": f"test {i}",
                      "cost": 0.04, "model": "imagen-4.0", "ratio": "1:1"},
                headers={"X-Session-Token": sess})
            assert r.status_code == 200
        # Verify
        r = client.get(f"/api/projects/{pid}",
            headers={"X-Session-Token": sess})
        body = r.get_json()
        assert len(body["generations"]) == 2
        assert body["generations"][0]["url"] == "/image/test-0.png"
        assert body["generations"][0]["prompt"] == "test 0"
        # First generation becomes the hero
        assert body["hero_url"] == "/image/test-0.png"

    def test_add_generation_requires_url(self, cs):
        client = cs.app.test_client()
        sess = _signup_and_login(client, "no-url@x.co")
        r = client.post("/api/projects", json={"name": "P"},
            headers={"X-Session-Token": sess})
        pid = r.get_json()["id"]
        r = client.post(f"/api/projects/{pid}/generations",
            json={"prompt": "no url"},
            headers={"X-Session-Token": sess})
        assert r.status_code == 400

    def test_add_to_nonexistent_returns_404(self, cs):
        client = cs.app.test_client()
        sess = _signup_and_login(client, "add404@x.co")
        r = client.post(f"/api/projects/nonexistent/generations",
            json={"url": "/image/x.png"},
            headers={"X-Session-Token": sess})
        assert r.status_code == 404


# ─── Export to zip ──────────────────────────────────────────────────

class TestExport:
    def test_export_returns_a_zip(self, cs, tmp_path):
        client = cs.app.test_client()
        sess = _signup_and_login(client, "exporter@x.co")
        # Create a project
        r = client.post("/api/projects", json={"name": "Launch campaign"},
            headers={"X-Session-Token": sess})
        pid = r.get_json()["id"]
        # Add a generation pointing to a real local image so it
        # actually gets embedded
        img_path = Path(cs.OUTPUT_DIR) / "2026-06-15" / "test_export.png"
        img_path.parent.mkdir(parents=True, exist_ok=True)
        img_path.write_bytes(b"fake-png-bytes-for-test")
        client.post(f"/api/projects/{pid}/generations",
            json={"url": f"/image/{img_path.relative_to(cs.OUTPUT_DIR)}",
                  "prompt": "export test"},
            headers={"X-Session-Token": sess})

        # Now export
        r = client.get(f"/api/projects/{pid}/export",
            headers={"X-Session-Token": sess})
        assert r.status_code == 200
        assert r.headers.get("Content-Type") == "application/zip"
        cd = r.headers.get("Content-Disposition", "")
        assert "attachment" in cd
        assert ".zip" in cd

        # Parse the zip
        z = zipfile.ZipFile(io.BytesIO(r.data))
        names = z.namelist()
        assert "manifest.json" in names
        # Image should be embedded
        img_entries = [n for n in names if n.startswith("images/")]
        assert len(img_entries) == 1
        # The embedded image is the one we wrote
        assert z.read(img_entries[0]) == b"fake-png-bytes-for-test"

        # Manifest is valid JSON with the project data
        manifest = json.loads(z.read("manifest.json"))
        assert manifest["id"] == pid
        assert manifest["name"] == "Launch campaign"
        assert len(manifest["generations"]) == 1
        assert manifest["generations"][0]["prompt"] == "export test"

    def test_export_skips_external_urls(self, cs):
        """For external URLs we don't fetch (could be slow / blocked)
        — the manifest still records them but the images/ folder is
        not populated."""
        client = cs.app.test_client()
        sess = _signup_and_login(client, "exporter-ext@x.co")
        r = client.post("/api/projects", json={"name": "External"},
            headers={"X-Session-Token": sess})
        pid = r.get_json()["id"]
        client.post(f"/api/projects/{pid}/generations",
            json={"url": "https://example.com/foo.png", "prompt": "ext"},
            headers={"X-Session-Token": sess})
        r = client.get(f"/api/projects/{pid}/export",
            headers={"X-Session-Token": sess})
        z = zipfile.ZipFile(io.BytesIO(r.data))
        img_entries = [n for n in z.namelist() if n.startswith("images/")]
        assert len(img_entries) == 0
        # But the manifest still has the URL
        manifest = json.loads(z.read("manifest.json"))
        assert manifest["generations"][0]["url"] == "https://example.com/foo.png"

    def test_export_cross_user_returns_404(self, cs):
        client = cs.app.test_client()
        sess_a = _signup_and_login(client, "ex-a@x.co")
        sess_b = _signup_and_login(client, "ex-b@x.co")
        r = client.post("/api/projects", json={"name": "A"},
            headers={"X-Session-Token": sess_a})
        pid = r.get_json()["id"]
        r = client.get(f"/api/projects/{pid}/export",
            headers={"X-Session-Token": sess_b})
        assert r.status_code == 404


# ─── Cap ───────────────────────────────────────────────────────────

class TestProjectCap:
    def test_user_capped_at_200_projects(self, cs, monkeypatch):
        """Oldest project gets deleted when the user hits the cap."""
        monkeypatch.setattr(cs, "_PROJECTS_MAX_PER_USER", 3)
        client = cs.app.test_client()
        sess = _signup_and_login(client, "capped@x.co")
        ids = []
        for i in range(4):
            r = client.post("/api/projects", json={"name": f"P{i}"},
                headers={"X-Session-Token": sess})
            ids.append(r.get_json()["id"])
        # Only 3 should exist now
        r = client.get("/api/projects", headers={"X-Session-Token": sess})
        assert len(r.get_json()["projects"]) == 3
        # The oldest (P0) is gone
        names = [p["name"] for p in r.get_json()["projects"]]
        assert "P0" not in names
        assert "P1" in names
        assert "P3" in names


# ─── Helper functions ───────────────────────────────────────────────

class TestHelpers:
    def test_parse_generations_json_handles_corrupt(self, cs):
        assert cs._parse_generations_json("") == []
        assert cs._parse_generations_json("not json") == []
        assert cs._parse_generations_json('"a string"') == []  # not a list
        assert cs._parse_generations_json('[{"a": 1}]') == [{"a": 1}]

    def test_serialize_project_row_includes_generations_by_default(self, cs):
        """Insert a real user first (FK constraint), then a project
        for that user. SELECT the row back so we have a Row object
        (INSERT ... RETURNING isn't a thing in older SQLite)."""
        with cs._auth_db() as db:
            db.execute(
                """INSERT INTO users (id, email, created_at, credits_remaining)
                   VALUES (?, ?, ?, 0)""",
                ("test-user", "t@x.co", "2026-06-15 13:00:00"))
            db.execute(
                """INSERT INTO projects (id, user_id, name, hero_url, generations_json,
                                         source_session_id, created_at, updated_at)
                   VALUES (?, ?, 'n', NULL, '[]', NULL, 't1', 't1')""",
                ("test-proj", "test-user"),
            )
            db.commit()
            row = db.execute("SELECT * FROM projects WHERE id = ?", ("test-proj",)).fetchone()
        assert row is not None
        out = cs._serialize_project_row(row)
        assert "generations" in out
        assert out["generations"] == []

    def test_create_project_caps_name_length(self, cs):
        long_name = "x" * 500
        proj = cs._create_project("user-1", long_name)
        assert len(proj["name"]) == 200
