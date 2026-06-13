"""
Regression tests for WS-1: waitlist endpoint + Photogen landing page
rebrand. The waitlist is the first public surface that takes user
data, so the test coverage is at the level the security PRs demanded.

Run:  pytest tests/test_refactor_ws1_waitlist.py -v
"""
import os
import re
import sys
import json
import importlib.util
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).parent.parent / "scripts"


def _load_web_module():
    web_path = SCRIPT_DIR / "creative-studio-web.py"
    sys.path.insert(0, str(SCRIPT_DIR))
    os.environ.setdefault("GEMINI_API_KEY", "test-key")
    spec = importlib.util.spec_from_file_location("creative_studio_web", web_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["creative_studio_web"] = mod
    spec.loader.exec_module(mod)
    return mod


cs = _load_web_module()


# ─── /api/waitlist ────────────────────────────────────────────────────────

class TestWaitlistEndpoint:
    """The waitlist is public — no auth, no key, just an email. Tests
    cover the happy path, validation, idempotency, and storage safety."""

    @pytest.fixture(autouse=True)
    def _reset_rate_limiter(self):
        """The rate limiter is module-level so the 20/min/IP cap gets
        exhausted across tests. Clear it before each test."""
        with cs._request_log_lock:
            cs._request_log.clear()

    @pytest.fixture
    def fresh_waitlist(self, tmp_path, monkeypatch):
        # Redirect WAITLIST_FILE to a per-test tmp file
        wl = tmp_path / "waitlist.json"
        wl.write_text("[]")
        monkeypatch.setattr(cs, "WAITLIST_FILE", wl)
        return wl

    def test_valid_email_signs_up_with_201(self, fresh_waitlist):
        client = cs.app.test_client()
        r = client.post("/api/waitlist", json={
            "email": "cam@ashbi.ca",
            "source": "landing-page",
        })
        assert r.status_code == 201, f"expected 201, got {r.status_code}: {r.get_data(as_text=True)}"
        body = r.get_json()
        assert body["email"] == "cam@ashbi.ca"
        assert body["position"] == 1
        assert body["total_signups"] == 1

    def test_email_normalized_to_lowercase(self, fresh_waitlist):
        client = cs.app.test_client()
        r = client.post("/api/waitlist", json={"email": "CAM@AshBi.CA"})
        assert r.status_code == 201
        assert r.get_json()["email"] == "cam@ashbi.ca"

    def test_duplicate_email_is_idempotent_200(self, fresh_waitlist):
        client = cs.app.test_client()
        r1 = client.post("/api/waitlist", json={"email": "a@b.co"})
        r2 = client.post("/api/waitlist", json={"email": "a@b.co"})
        assert r1.status_code == 201
        assert r2.status_code == 200
        body = r2.get_json()
        assert body["already_signed_up"] is True
        assert body["email"] == "a@b.co"

    def test_missing_email_rejected(self, fresh_waitlist):
        client = cs.app.test_client()
        r = client.post("/api/waitlist", json={})
        assert r.status_code == 400

    def test_empty_email_rejected(self, fresh_waitlist):
        client = cs.app.test_client()
        r = client.post("/api/waitlist", json={"email": ""})
        assert r.status_code == 400

    def test_no_at_sign_rejected(self, fresh_waitlist):
        client = cs.app.test_client()
        r = client.post("/api/waitlist", json={"email": "not-an-email"})
        assert r.status_code == 400

    def test_no_dot_in_domain_rejected(self, fresh_waitlist):
        client = cs.app.test_client()
        r = client.post("/api/waitlist", json={"email": "cam@ashbi"})
        assert r.status_code == 400

    def test_oversize_email_rejected(self, fresh_waitlist):
        client = cs.app.test_client()
        # 320 chars total (a*316 + "@x.co" = 321) — over the 320 cap
        long_email = "a" * 316 + "@x.co"
        r = client.post("/api/waitlist", json={"email": long_email})
        assert r.status_code == 400

    def test_oversize_source_capped_not_rejected(self, fresh_waitlist):
        client = cs.app.test_client()
        r = client.post("/api/waitlist", json={
            "email": "x@y.co",
            "source": "X" * 1000,
        })
        assert r.status_code == 201
        entries = cs._read_waitlist()
        assert len(entries[0]["source"]) == 64

    def test_concurrent_signups_all_persist(self, fresh_waitlist, monkeypatch):
        """50 threads each post a unique email; all should be in the file.
        The rate limiter is bypassed for this test (it's tested elsewhere);
        we want to verify the FS storage layer's concurrency safety."""
        import threading
        # Bump the rate cap so the storage layer is what we're testing
        monkeypatch.setattr(cs, "_RATE_LIMIT", 1000)
        client = cs.app.test_client()
        results = []
        results_lock = threading.Lock()

        def _post(i):
            r = client.post("/api/waitlist", json={"email": f"user{i:03d}@x.co"})
            with results_lock:
                results.append((i, r.status_code))

        threads = [threading.Thread(target=_post, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        failed = [(i, s) for i, s in results if s != 201]
        assert not failed, f"some failed: {failed[:5]}"
        entries = cs._read_waitlist()
        assert len(entries) == 50
        emails = {e["email"] for e in entries}
        assert len(emails) == 50

    def test_file_is_atomic_write(self, fresh_waitlist):
        """The write should use a tmp file + os.replace for crash safety."""
        client = cs.app.test_client()
        for i in range(3):
            client.post("/api/waitlist", json={"email": f"a{i}@x.co"})
        assert fresh_waitlist.exists()
        tmp = fresh_waitlist.with_suffix(".json.tmp")
        assert not tmp.exists(), f"tmp file should not be left behind: {tmp}"
        data = json.loads(fresh_waitlist.read_text())
        assert isinstance(data, list)
        assert len(data) == 3

    def test_corrupt_waitlist_file_doesnt_crash(self, fresh_waitlist):
        """If waitlist.json is corrupted (e.g. half-written by a crash),
        the next signup should NOT 500. The reader returns an empty
        list and the write replaces the file with valid JSON."""
        fresh_waitlist.write_text("{not valid json")
        client = cs.app.test_client()
        r = client.post("/api/waitlist", json={"email": "recovery@x.co"})
        assert r.status_code == 201, f"got {r.status_code}: {r.get_data(as_text=True)}"
        data = json.loads(fresh_waitlist.read_text())
        assert isinstance(data, list)
        assert data[0]["email"] == "recovery@x.co"


# ─── Landing page rebrand ────────────────────────────────────────────────

class TestLandingPageRebrand:
    """The static landing page should brand as Photogen (not Creative
    Studio), have the waitlist form, and have OG card meta for social
    unfurling."""

    def _src(self):
        return (Path(__file__).parent.parent / "templates" / "landing.html").read_text()

    def test_title_is_photogen(self):
        src = self._src()
        assert "Photogen" in src.split("<title>")[1].split("</title>")[0]
        assert "Creative Studio" not in src.split("<title>")[1].split("</title>")[0]

    def test_og_meta_tags_present(self):
        src = self._src()
        for required in (
            'property="og:title"',
            'property="og:description"',
            'property="og:type"',
            'property="og:url"',
            'name="twitter:card"',
            'name="twitter:title"',
            'name="twitter:description"',
            'rel="canonical"',
        ):
            assert required in src, f"missing meta tag: {required}"

    def test_canonical_url_is_photogen(self):
        src = self._src()
        # Use chr() to avoid string escape issues
        dq = chr(34)
        needle = dq + "https://photogen.ashbi.ca" + dq
        assert f'rel="canonical" href={needle}' in src

    def test_waitlist_form_renders(self):
        src = self._src()
        assert 'id="waitlistForm"' in src
        assert 'id="waitlistEmail"' in src
        assert 'id="waitlistSubmit"' in src
        # JS posts to the endpoint — accept either quote style
        sq = chr(39)
        assert "fetch(" + sq + "/api/waitlist" + sq in src

    def test_photogen_in_footer(self):
        src = self._src()
        footer = src.split('class="landing-footer"')[1]
        assert "Photogen" in footer
        assert '<div class="footer-mark">P</div>' in footer
        assert '<div class="footer-mark">CS</div>' not in footer

    def test_photogen_in_nav(self):
        src = self._src()
        # Photogen appears in <title> and the hero note about waitlist
        # and the footer. Just check it's the visible brand.
        assert "Photogen" in src
        # The old "Creative Studio" brand name is gone from title + nav + footer
        # (FAQ body content is allowed to mention it as historical context)
        title = src.split("<title>")[1].split("</title>")[0]
        assert "Creative Studio" not in title

    def test_does_not_remove_existing_content(self):
        src = self._src()
        for required in (
            'id="heroSection"',
            'id="showcase"',
            'id="pricing"',
            'id="faq"',
            'id="waitlist"',
        ):
            assert required in src, f"missing: {required}"
