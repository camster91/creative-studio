"""
Regression tests for /admin/waitlist + /admin/waitlist.csv (the
operator-only view of the lead-capture file).

The admin surface is shared-secret auth (X-Admin-Secret header vs
PHOTOGEN_ADMIN_SECRET env var). The endpoint fail-closes when the
env var is empty — every request returns 401. This is for a
single-operator side project, not multi-tenant.

Run:  pytest tests/test_refactor_ws1 admin.py -v
"""
import os
import sys
import importlib.util
import tempfile
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).parent.parent / "scripts"


def _load_web_module(admin_secret=""):
    web_path = SCRIPT_DIR / "creative-studio-web.py"
    sys.path.insert(0, str(SCRIPT_DIR))
    if admin_secret:
        os.environ["PHOTOGEN_ADMIN_SECRET"] = admin_secret
    elif "PHOTOGEN_ADMIN_SECRET" in os.environ:
        del os.environ["PHOTOGEN_ADMIN_SECRET"]
    os.environ.setdefault("GEMINI_API_KEY", "test-key")
    spec = importlib.util.spec_from_file_location("creative_studio_web", web_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["creative_studio_web"] = mod
    spec.loader.exec_module(mod)
    return mod


# ─── /admin/waitlist HTML view ───────────────────────────────────────────

class TestAdminWaitlistAuth:
    """Fail-closed: no env var = every request 401s."""

    def test_no_admin_secret_env_returns_401(self, tmp_path, monkeypatch):
        # Make sure the env var is unset
        monkeypatch.delenv("PHOTOGEN_ADMIN_SECRET", raising=False)
        # Re-load the module to pick up the unset env (ADMIN_SECRET is read at import)
        cs = _load_web_module("")
        client = cs.app.test_client()
        r = client.get("/admin/waitlist")
        assert r.status_code == 401
        assert "PHOTOGEN_ADMIN_SECRET" in r.get_data(as_text=True)

    def test_wrong_secret_returns_401(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PHOTOGEN_ADMIN_SECRET", "correct-secret-1234")
        cs = _load_web_module("correct-secret-1234")
        client = cs.app.test_client()
        r = client.get("/admin/waitlist",
            headers={"X-Admin-Secret": "wrong-secret"})
        assert r.status_code == 401

    def test_no_header_returns_401(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PHOTOGEN_ADMIN_SECRET", "secret")
        cs = _load_web_module("secret")
        client = cs.app.test_client()
        r = client.get("/admin/waitlist")
        assert r.status_code == 401

    def test_correct_secret_returns_200(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PHOTOGEN_ADMIN_SECRET", "secret")
        cs = _load_web_module("secret")
        # Redirect WAITLIST_FILE to per-test tmp
        wl = tmp_path / "waitlist.json"
        wl.write_text("[]")
        monkeypatch.setattr(cs, "WAITLIST_FILE", wl)
        client = cs.app.test_client()
        r = client.get("/admin/waitlist",
            headers={"X-Admin-Secret": "secret"})
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "<h1>Photogen Waitlist</h1>" in body
        assert "No signups yet" in body  # empty list renders this


# ─── /admin/waitlist rendering ──────────────────────────────────────────

class TestAdminWaitlistRender:
    """When authed, the page renders a sensible table with summary."""

    def test_renders_email_and_source(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PHOTOGEN_ADMIN_SECRET", "s")
        cs = _load_web_module("s")
        wl = tmp_path / "waitlist.json"
        wl.write_text('[{"email":"a@b.co","source":"landing-page","ts":"2026-06-13 13:00:00"}]')
        monkeypatch.setattr(cs, "WAITLIST_FILE", wl)
        client = cs.app.test_client()
        r = client.get("/admin/waitlist", headers={"X-Admin-Secret": "s"})
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "a@b.co" in body
        assert "landing-page" in body
        assert "2026-06-13 13:00:00" in body
        assert "1" in body  # total signups

    def test_renders_source_summary(self, tmp_path, monkeypatch):
        """The page shows a per-source count table so the operator
        can see which acquisition channels are producing signups."""
        monkeypatch.setenv("PHOTOGEN_ADMIN_SECRET", "s")
        cs = _load_web_module("s")
        wl = tmp_path / "waitlist.json"
        wl.write_text("""[
            {"email":"a@b.co","source":"landing-page","ts":"t1"},
            {"email":"c@d.co","source":"landing-page","ts":"t2"},
            {"email":"e@f.co","source":"twitter","ts":"t3"}
        ]""")
        monkeypatch.setattr(cs, "WAITLIST_FILE", wl)
        client = cs.app.test_client()
        r = client.get("/admin/waitlist", headers={"X-Admin-Secret": "s"})
        body = r.get_data(as_text=True)
        assert "landing-page" in body
        assert "twitter" in body
        assert "3" in body  # total

    def test_csv_download_link_present(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PHOTOGEN_ADMIN_SECRET", "s")
        cs = _load_web_module("s")
        wl = tmp_path / "waitlist.json"
        wl.write_text("[]")
        monkeypatch.setattr(cs, "WAITLIST_FILE", wl)
        client = cs.app.test_client()
        r = client.get("/admin/waitlist", headers={"X-Admin-Secret": "s"})
        body = r.get_data(as_text=True)
        assert "/admin/waitlist.csv" in body


# ─── /admin/waitlist.csv ─────────────────────────────────────────────────

class TestAdminWaitlistCSV:
    def test_csv_has_header_and_rows(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PHOTOGEN_ADMIN_SECRET", "s")
        cs = _load_web_module("s")
        wl = tmp_path / "waitlist.json"
        wl.write_text("""[
            {"email":"a@b.co","source":"landing-page","ts":"2026-06-13 13:00:00"},
            {"email":"c@d.co","source":"twitter","ts":"2026-06-13 13:05:00"}
        ]""")
        monkeypatch.setattr(cs, "WAITLIST_FILE", wl)
        client = cs.app.test_client()
        r = client.get("/admin/waitlist.csv", headers={"X-Admin-Secret": "s"})
        assert r.status_code == 200
        assert r.headers.get("Content-Type", "").startswith("text/csv")
        assert "attachment" in r.headers.get("Content-Disposition", "")
        body = r.get_data(as_text=True)
        lines = body.strip().split("\n")
        assert lines[0] == "timestamp,email,source"
        assert "a@b.co,landing-page" in lines[1]
        assert "c@d.co,twitter" in lines[2]

    def test_csv_empty_returns_header_only(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PHOTOGEN_ADMIN_SECRET", "s")
        cs = _load_web_module("s")
        wl = tmp_path / "waitlist.json"
        wl.write_text("[]")
        monkeypatch.setattr(cs, "WAITLIST_FILE", wl)
        client = cs.app.test_client()
        r = client.get("/admin/waitlist.csv", headers={"X-Admin-Secret": "s"})
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert body.strip() == "timestamp,email,source"

    def test_csv_requires_auth(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PHOTOGEN_ADMIN_SECRET", "s")
        cs = _load_web_module("s")
        client = cs.app.test_client()
        r = client.get("/admin/waitlist.csv")  # no header
        assert r.status_code == 401


# ─── Rate-limited like everything else ───────────────────────────────────

class TestAdminRateLimited:
    def test_admin_waitlist_uses_rate_limiter(self, tmp_path, monkeypatch):
        """The admin endpoint goes through the same @rate_limited
        wrapper, so a brute-force header guess gets capped at 20/min/IP."""
        monkeypatch.setenv("PHOTOGEN_ADMIN_SECRET", "s")
        cs = _load_web_module("s")
        wl = tmp_path / "waitlist.json"
        wl.write_text("[]")
        monkeypatch.setattr(cs, "WAITLIST_FILE", wl)
        # Bump rate cap so the test isn't flaky
        monkeypatch.setattr(cs, "_RATE_LIMIT", 1000)
        client = cs.app.test_client()
        # 30 wrong attempts shouldn't 429
        for _ in range(30):
            client.get("/admin/waitlist",
                headers={"X-Admin-Secret": "wrong"})
        # But the correct secret should still work
        r = client.get("/admin/waitlist", headers={"X-Admin-Secret": "s"})
        assert r.status_code == 200
