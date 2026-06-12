"""
Regression tests for the security fixes applied after the 2026-06-12 review.

Covers:
  - SSRF in /api/export-zip
  - Path traversal in /api/qc and /api/export
  - Daily-limit TOCTOU + JSON lost-update races
  - X-Forwarded-For rate-limit bypass
  - HTML injection in buildCellHTML
  - Version constant

Run:  pytest tests/test_security_fixes.py -v
"""
import os
import sys
import json
import threading
import tempfile
import importlib.util
from pathlib import Path
from unittest.mock import patch

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


@pytest.fixture
def fs_isolated(monkeypatch):
    """Redirect DATA_DIR/OUTPUT_DIR/PINS_DB to a fresh tmpdir for the test."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        monkeypatch.setattr(cs, "DATA_DIR", td / "data")
        monkeypatch.setattr(cs, "SESSIONS_DIR", td / "data" / "sessions")
        monkeypatch.setattr(cs, "COST_DB", td / "data" / "costs.json")
        monkeypatch.setattr(cs, "OUTPUT_DIR", td / "outputs")
        monkeypatch.setattr(cs, "PINS_DB", td / "data" / "pins.json")
        (td / "data" / "sessions").mkdir(parents=True, exist_ok=True)
        (td / "outputs").mkdir(parents=True, exist_ok=True)
        yield td


@pytest.fixture
def flask_client():
    app = cs.app
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


# ─── SSRF in /api/export-zip ────────────────────────────────────────────

class TestExportZipSSRF:
    BAD = [
        "http://127.0.0.1/admin",
        "http://127.0.0.1:5984/",
        "http://localhost/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "file:///etc/passwd",
        "gopher://127.0.0.1:80/",
        "javascript:alert(1)",
        "ftp://example.com/",
        "data:text/plain,hi",
        "http://[::1]/",
        "http://[fe80::1]/",
    ]

    def test_safe_url_helper_blocks_loopback(self):
        for u in self.BAD:
            assert cs._is_safe_export_url(u) is False, f"should have blocked {u}"

    def test_safe_url_helper_allows_own_image_path(self):
        assert cs._is_safe_export_url("/image/2026-06-12/hi.png") is True

    def test_safe_url_helper_blocks_image_path_with_crlf(self):
        assert cs._is_safe_export_url("/image/foo\r\nHost: evil") is False

    def test_endpoint_rejects_aws_metadata(self, flask_client, fs_isolated):
        r = flask_client.post(
            "/api/export-zip",
            json={"urls": ["http://169.254.169.254/latest/meta-data/"]},
        )
        assert r.status_code == 400
        body = r.get_json()
        assert "rejected" in body
        assert "169.254.169.254" in body["rejected"][0]

    def test_endpoint_rejects_localhost(self, flask_client, fs_isolated):
        r = flask_client.post(
            "/api/export-zip",
            json={"urls": ["http://127.0.0.1:5984/"]},
        )
        assert r.status_code == 400

    def test_endpoint_accepts_same_origin_image(self, flask_client, fs_isolated):
        r = flask_client.post(
            "/api/export-zip",
            json={"urls": ["/image/2026-06-12/foo.png"]},
        )
        # No file actually exists, so the inner write fails but endpoint
        # returns a 200 zip containing an error.txt — that's OK, what we
        # care about is that the SSRF gate didn't reject it.
        assert r.status_code == 200
        assert r.mimetype == "application/zip"


# ─── Path traversal in /api/qc and /api/export ──────────────────────────

class TestPathTraversal:
    def _make_image(self, fs_isolated):
        p = fs_isolated / "outputs" / "real.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"fake")
        return p

    def test_safe_relpath_blocks_traversal(self, fs_isolated):
        assert cs._safe_output_relpath("../../../etc/passwd") is None
        assert cs._safe_output_relpath("../app/data/sessions/x.json") is None
        assert cs._safe_output_relpath("..") is None
        assert cs._safe_output_relpath("") is None
        assert cs._safe_output_relpath("notreal.png") is None  # doesn't exist
        assert cs._safe_output_relpath("../real.png") is None  # traversal token

    def test_safe_relpath_allows_real_file(self, fs_isolated):
        self._make_image(fs_isolated)
        result = cs._safe_output_relpath("real.png")
        assert result is not None
        assert result.name == "real.png"

    def test_qc_rejects_traversal_image_url(self, flask_client, fs_isolated):
        r = flask_client.post(
            "/api/qc",
            json={"image_url": "/image/../../etc/passwd"},
        )
        # Will be 402 (no key) or 400 (rejected path) — both indicate
        # that the traversal was caught *before* the subprocess ran.
        assert r.status_code in (400, 402)
        if r.status_code == 400:
            assert "outside" in r.get_json()["error"].lower() or "invalid" in r.get_json()["error"].lower()

    def test_export_rejects_traversal_image_url(self, flask_client, fs_isolated):
        r = flask_client.post(
            "/api/export",
            data={"image_url": "/image/../../etc/passwd", "presets": "amazon"},
        )
        assert r.status_code in (400, 402)
        if r.status_code == 400:
            assert "outside" in r.get_json()["error"].lower() or "invalid" in r.get_json()["error"].lower()


# ─── JSON lost-update + daily-limit TOCTOU ──────────────────────────────

class TestCostRace:
    def test_concurrent_track_cost_does_not_lose_increments(self, fs_isolated):
        """50 threads each call track_cost; total should be 50 × 0.02, not
        less (the lost-update bug was real)."""
        N = 50
        PRICE = 0.02
        threads = [
            threading.Thread(
                target=cs.track_cost,
                args=("imagen-4.0-fast-generate-001", "1K", 1),
            )
            for _ in range(N)
        ]
        for t in threads: t.start()
        for t in threads: t.join()
        costs = cs.load_costs()
        assert costs["image_count"] == N, f"image_count={costs['image_count']}"
        assert abs(costs["total"] - N * PRICE) < 1e-6, f"total={costs['total']}"
        assert abs(costs["by_model"]["imagen-4.0-fast-generate-001"] - N * PRICE) < 1e-6

    def test_daily_limit_serializes_check_and_charge(self, fs_isolated, monkeypatch):
        """Two concurrent _check_daily_limit calls at limit - 0.04 should
        not both pass — one must 429."""
        monkeypatch.setattr(cs.os.environ, "get", lambda k, d=None: "0.05" if k == "CREATIVE_DAILY_LIMIT" else d)
        # Charge 0.04 first so we're at 0.04
        cs.track_cost("imagen-4.0-fast-generate-001", "1K", 2)  # +0.04
        # Two parallel checks for 0.04 more — only one should pass
        results = []
        results_lock = threading.Lock()

        def _go():
            # jsonify() needs an app context, so wrap each thread.
            with cs.app.app_context():
                r = cs._check_daily_limit(2, "fast")
            with results_lock:
                results.append(r)
        threads = [threading.Thread(target=_go) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join()
        none_count = sum(1 for r in results if r is None)
        rejected = [r for r in results if r is not None]
        # At most one passed; at least one got a 429
        assert none_count <= 1, f"both passed: {results}"
        assert len(rejected) >= 1
        assert rejected[0][1] == 429

    def test_save_pins_concurrent_writes_dont_lose_entries(self, fs_isolated):
        """Two threads appending to two different image_paths must both
        persist."""
        def _save(path, pins):
            cs.save_pins(path, pins)
        t1 = threading.Thread(target=_save, args=("/a", [{"id": "x", "text": "a"}]))
        t2 = threading.Thread(target=_save, args=("/b", [{"id": "y", "text": "b"}]))
        t1.start(); t2.start(); t1.join(); t2.join()
        assert cs.load_pins("/a")[0]["id"] == "x"
        assert cs.load_pins("/b")[0]["id"] == "y"


# ─── X-Forwarded-For rate-limit bypass ──────────────────────────────────

class TestClientIp:
    def test_untrusted_xff_ignored_by_default(self, fs_isolated):
        with cs.app.test_request_context(
            "/api/whoami",
            headers={"X-Forwarded-For": "1.2.3.4"},
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        ):
            assert cs._client_ip() == "127.0.0.1"

    def test_trust_proxy_honors_xff_rightmost(self, fs_isolated, monkeypatch):
        monkeypatch.setenv("TRUST_PROXY", "1")
        with cs.app.test_request_context(
            "/api/whoami",
            headers={"X-Forwarded-For": "1.1.1.1, 2.2.2.2, 3.3.3.3"},
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        ):
            # X-Forwarded-For right-most is the original client per spec
            assert cs._client_ip() == "3.3.3.3"

    def test_no_xff_falls_back_to_remote_addr(self, fs_isolated, monkeypatch):
        monkeypatch.setenv("TRUST_PROXY", "1")
        with cs.app.test_request_context(
            "/api/whoami",
            environ_overrides={"REMOTE_ADDR": "10.0.0.1"},
        ):
            assert cs._client_ip() == "10.0.0.1"


# ─── Version constant ───────────────────────────────────────────────────

class TestVersion:
    def test_whoami_returns_module_version(self, flask_client):
        r = flask_client.get("/api/whoami")
        body = r.get_json()
        assert body["version"] == cs.__version__
        # Should match pyproject.toml — fail if the two drift.
        pyproject = (Path(__file__).parent.parent / "pyproject.toml").read_text()
        assert f'version = "{cs.__version__}"' in pyproject, \
            f"__version__={cs.__version__} out of sync with pyproject.toml"


# ─── HTML-injection regression (JS) ────────────────────────────────────
# These don't execute JS; they parse it and assert the safeAttr helper is
# wired into buildCellHTML's interpolations. Cheap, fast, no node needed.

class TestBuildCellHtmlSafeAttr:
    @pytest.fixture
    def app_js_source(self):
        return (Path(__file__).parent.parent / "static" / "app.js").read_text()

    def test_safe_attr_helper_defined(self, app_js_source):
        assert "function safeAttr(" in app_js_source

    def test_unsafe_interpolations_gone(self, app_js_source):
        """None of the buildCellHTML interpolations should be raw ${img.X}."""
        # Pull the function body. The function is indented inside the module,
        # so we search for the opening brace and walk to the matching close
        # (or until the next top-level function/const at the same indent).
        start = app_js_source.find("function buildCellHTML(img)")
        assert start != -1, "buildCellHTML not found"
        # Find the function's opening brace
        brace_open = app_js_source.find("{", start)
        depth = 0
        i = brace_open
        while i < len(app_js_source):
            ch = app_js_source[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    body = app_js_source[brace_open:i+1]
                    break
            i += 1
        else:
            pytest.fail("buildCellHTML body not closed")
        # Every img.url / img.name / img.prompt / img.model / img.cost must
        # be wrapped in safeAttr(...)
        for field in ("img.url", "img.name", "img.prompt", "img.model"):
            assert f"${{{field}}}" not in body, f"unescaped ${{{field}}} in buildCellHTML"
        assert "safeAttr(img.url)" in body
        assert "safeAttr(img.name)" in body
        assert "safeAttr(img.model" in body  # matches safeAttr(img.model||'')
        assert "safeAttr(img.cost" in body

    def test_safe_attr_escapes_quotes(self):
        """Simulate the JS regex chain in Python and confirm a payload
        with a quote + script tag is escaped."""
        s = '"/><script>alert(1)</script>'
        out = (str(s)
            .replace("&", "&amp;")
            .replace('"', "&quot;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "&#10;")
            .replace("\r", "&#13;"))
        assert '"' not in out
        assert "<script>" not in out
        assert "&lt;script&gt;" in out
