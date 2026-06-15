"""
Regression tests for WS-2b: trial-credit auth flow.

Run:  pytest tests/test_refactor_ws2b_trial_credits.py -v
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).parent.parent / "scripts"


def _load_module(tmp_path, server_api_key="test-server-key",
                allow_fallback=True):
    web_path = SCRIPT_DIR / "creative-studio-web.py"
    sys.path.insert(0, str(SCRIPT_DIR))
    os.environ["GEMINI_API_KEY"] = server_api_key
    os.environ.setdefault("PHOTOGEN_ADMIN_SECRET", "")
    spec = importlib.util.spec_from_file_location("creative_studio_web", web_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["creative_studio_web"] = mod
    spec.loader.exec_module(mod)
    mod.AUTH_DB = tmp_path / "users.db"
    mod._init_auth_schema()
    mod.SERVER_API_KEY = server_api_key
    mod.ALLOW_SERVER_FALLBACK = allow_fallback
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


# ─── _require_api_key: auth resolution order ───────────────────────────

class TestRequireApiKey:
    def test_header_key_takes_priority(self, cs):
        """X-API-Key header set: no credit burned even if user is signed in."""
        client = cs.app.test_client()
        sess = _signup_and_login(client, "header-first@x.co")
        client.post("/api/generate",
            json={"prompt": "x", "tier": "fast", "aspect_ratio": "1:1"},
            headers={"X-API-Key": "AIzaFromHeader", "X-Session-Token": sess})
        with cs._auth_db() as db:
            user = db.execute("SELECT * FROM users WHERE email = ?", ("header-first@x.co",)).fetchone()
            assert user["credits_remaining"] == 5

    def test_signed_in_no_server_fallback_burns_credit(self, tmp_path):
        """No X-API-Key + signed in + server fallback disabled -> credit burned."""
        cs = _load_module(tmp_path, server_api_key="", allow_fallback=False)
        client = cs.app.test_client()
        sess = _signup_and_login(client, "burns-credit@x.co")
        client.post("/api/generate",
            json={"prompt": "x", "tier": "fast", "aspect_ratio": "1:1"},
            headers={"X-Session-Token": sess})
        with cs._auth_db() as db:
            user = db.execute("SELECT * FROM users WHERE email = ?", ("burns-credit@x.co",)).fetchone()
            assert user["credits_remaining"] == 4
            assert user["credits_used_today"] == 1

    def test_signed_in_with_server_fallback_does_not_burn(self, cs):
        """No X-API-Key + signed in + server fallback ON -> no credit burned.
        Server provides the key, same as anonymous demo. Credits are
        only burned in self-hosted mode with no server key."""
        client = cs.app.test_client()
        sess = _signup_and_login(client, "no-burn@x.co")
        client.post("/api/generate",
            json={"prompt": "x", "tier": "fast", "aspect_ratio": "1:1"},
            headers={"X-Session-Token": sess})
        with cs._auth_db() as db:
            user = db.execute("SELECT * FROM users WHERE email = ?", ("no-burn@x.co",)).fetchone()
            assert user["credits_remaining"] == 5
            assert user["credits_used_today"] == 0

    def test_anonymous_no_server_fallback_returns_402(self, tmp_path):
        """Anonymous + no key + server fallback off -> 402."""
        cs = _load_module(tmp_path, server_api_key="", allow_fallback=False)
        client = cs.app.test_client()
        r = client.post("/api/generate",
            json={"prompt": "x", "tier": "fast", "aspect_ratio": "1:1"})
        assert r.status_code == 402
        assert r.get_json()["error"] == "BYOK or sign-in required"

    def test_credits_exhausted_no_server_key_returns_402_with_message(self, tmp_path):
        """Signed in + 0 credits + no server key -> 402 with
        'trial credits are used up' message."""
        cs = _load_module(tmp_path, server_api_key="", allow_fallback=False)
        client = cs.app.test_client()
        sess = _signup_and_login(client, "no-credits@x.co")
        with cs._auth_db() as db:
            db.execute("UPDATE users SET credits_remaining = 0 WHERE email = ?", ("no-credits@x.co",))
            db.commit()
        r = client.post("/api/generate",
            json={"prompt": "x", "tier": "fast", "aspect_ratio": "1:1"},
            headers={"X-Session-Token": sess})
        assert r.status_code == 402
        assert "used up" in r.get_json()["message"].lower()

    def test_5_then_6_returns_402(self, tmp_path):
        """5 trial credits -> 5 pass the gate, 6th 402. No server fallback."""
        cs = _load_module(tmp_path, server_api_key="", allow_fallback=False)
        client = cs.app.test_client()
        sess = _signup_and_login(client, "five-then-six@x.co")
        for i in range(5):
            r = client.post("/api/generate",
                json={"prompt": f"test-{i}", "tier": "fast", "aspect_ratio": "1:1"},
                headers={"X-Session-Token": sess})
            assert r.status_code != 402, f"call {i+1} got 402"
        r = client.post("/api/generate",
            json={"prompt": "test-6", "tier": "fast", "aspect_ratio": "1:1"},
            headers={"X-Session-Token": sess})
        assert r.status_code == 402
        assert "used up" in r.get_json()["message"].lower()


# ─── No-server-fallback edge case ───────────────────────────────────────

class TestNoServerFallback:
    def test_signed_in_with_credit_but_no_server_key_returns_500(self, tmp_path):
        """If credits are burned but SERVER_API_KEY is empty (degenerate
        config), return 500 with a clear error."""
        cs = _load_module(tmp_path, server_api_key="", allow_fallback=False)
        client = cs.app.test_client()
        sess = _signup_and_login(client, "no-server-key@x.co")
        r = client.post("/api/generate",
            json={"prompt": "x", "tier": "fast", "aspect_ratio": "1:1"},
            headers={"X-Session-Token": sess})
        assert r.status_code == 500
        body = r.get_json()
        assert "Trial credit burned" in body["error"] or "server-side" in body["error"]


# ─── Rate limiter still applies ─────────────────────────────────────────

class TestRateLimiterStillApplies:
    def test_burns_all_5_then_blocks_at_402(self, tmp_path, monkeypatch):
        cs = _load_module(tmp_path, server_api_key="", allow_fallback=False)
        monkeypatch.setattr(cs, "_RATE_LIMIT", 1000)
        client = cs.app.test_client()
        sess = _signup_and_login(client, "brute@x.co")
        statuses = []
        for _ in range(7):
            r = client.post("/api/generate",
                json={"prompt": "x", "tier": "fast", "aspect_ratio": "1:1"},
            headers={"X-Session-Token": sess})
            statuses.append(r.status_code)
        non_402_count = sum(1 for s in statuses if s != 402)
        assert non_402_count == 5, f"expected 5 non-402, got {non_402_count}: {statuses}"
        assert statuses[5] == 402
        assert statuses[6] == 402
