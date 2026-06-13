"""
Regression tests for the 2026-06-13 third-pass audit.

Covers:
  - PR #38 TOCTOU regression: enforce_daily_limit must be the locked alias
    (not a duplicate unlocked definition that Python silently shadows)
  - scene-set filename collision: run_cli_composite must add a unique
    suffix so 5 parallel scenes don't all write to the same file
  - 6 read endpoints now require _require_api_key():
    /api/jobs/<id>, /api/sessions, /api/session/<id>, /api/costs,
    /api/chat/<key>/history, /api/chat/<key>/reset

Run:  pytest tests/test_audit_2026_06_13_p3.py -v
"""
import os
import sys
import json
import importlib.util
import threading
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


# ─── Finding 1: PR #38 TOCTOU regression — enforce_daily_limit duplicate ─

class TestEnforceDailyLimitRegression:
    """The bug: PR #38 added a locked `def _check_daily_limit()` and a
    thin alias `def enforce_daily_limit()` that delegates to it. But the
    alias at the top of the file (line 321) was shadowed by a SECOND
    `def enforce_daily_limit()` further down (line 334) — the old
    unlocked version. Python keeps the second definition, so every
    call site in production was hitting the unlocked code and the
    TOCTOU bypass was live. The fix: delete the second definition.
    """

    def test_exactly_one_enforce_daily_limit_definition(self):
        """Static source check: there must be exactly one
        `def enforce_daily_limit` in the file."""
        src = (Path(__file__).parent.parent / "scripts" / "creative-studio-web.py").read_text()
        import re
        defs = re.findall(r"^def enforce_daily_limit\(", src, re.MULTILINE)
        assert len(defs) == 1, (
            f"Found {len(defs)} `def enforce_daily_limit` definitions in "
            f"creative-studio-web.py. Exactly one is required — the second "
            f"is the unlocked version from before PR #38 and silently "
            f"shadows the locked alias."
        )

    def test_enforce_daily_limit_delegates_to_check(self):
        """Identity check: enforce_daily_limit must be a wrapper that
        delegates to _check_daily_limit. If the duplicate is reintroduced,
        this fails."""
        import inspect
        assert cs.enforce_daily_limit is not cs._check_daily_limit, (
            "enforce_daily_limit must be a wrapper, not the same object as "
            "_check_daily_limit"
        )
        src = inspect.getsource(cs.enforce_daily_limit)
        assert "_check_daily_limit" in src, (
            f"enforce_daily_limit wrapper doesn't delegate to "
            f"_check_daily_limit. Got: {src[:200]}"
        )

    def test_concurrent_enforce_daily_limit_rejects_at_cap(self, monkeypatch):
        """Live TOCTOU behavior: 3 concurrent calls at 0.07/0.10 cap
        with each call wanting 0.04 → all 3 must be rejected. With the
        duplicate unlocked version, all 3 would pass (the bug)."""
        monkeypatch.setattr(cs.os.environ, "get",
            lambda k, d=None: "0.10" if k == "CREATIVE_DAILY_LIMIT" else d)
        # Pre-charge 0.07
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        cs._with_json_lock(lambda: cs.save_costs({
            "total": 0.07, "by_model": {}, "by_date": {today: 0.07},
            "session_count": 0, "image_count": 0,
        }))
        results = []
        results_lock = threading.Lock()
        def _go():
            with cs.app.app_context():
                r = cs.enforce_daily_limit(2, "fast")
            with results_lock:
                results.append(r)
        threads = [threading.Thread(target=_go) for _ in range(3)]
        for t in threads: t.start()
        for t in threads: t.join()
        rejected = [r for r in results if r is not None]
        assert len(rejected) == 3, (
            f"Expected 3/3 to be rejected (each +0.04 → 0.11 > 0.10 cap). "
            f"Got {len(rejected)}/3 rejected. This is the TOCTOU bypass. "
            f"results={[r[1] if r else None for r in results]}"
        )


# ─── Finding 2: scene-set filename collision ──────────────────────────

class TestSceneSetFilenameCollision:
    """The bug: run_cli_composite built filenames as
    `f"composite-{int(time.time())}.png"`. When 5 parallel scene-set
    threads all start within the same second, they all use the
    identical filename. The 5 subprocesses race-write; only the last
    one to finish survives. The user pays for 5 generations but
    receives 1-2.
    The fix: name_suffix parameter (or uuid) appended to the filename.
    """

    def test_run_cli_composite_has_unique_filename(self, monkeypatch):
        """Verify that calling run_cli_composite twice in the same
        second produces two distinct on-disk filenames."""
        import time as t
        # Patch time.time to be constant (both calls in the same second)
        monkeypatch.setattr(t, "time", lambda: 1700000000.0)
        monkeypatch.setattr(cs.time, "time", lambda: 1700000000.0)
        # Patch subprocess.run AND make it create the expected output
        # file so the run_cli_composite check `if out_path.exists()`
        # succeeds.
        class FakeResult:
            stdout = ""; stderr = ""; returncode = 0
        def _fake_run(args, **kw):
            # The 12th arg is the --filename value (after --prompt, prompt)
            # Actually the args list puts --filename at the end; the value
            # is at args[-1]. Extract the fname.
            fname = args[-1]
            out_dir = Path(cs.OUTPUT_DIR) / t.strftime("%Y-%m-%d") / "composite"
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / fname).write_bytes(b"fake")
            return FakeResult()
        monkeypatch.setattr(cs.subprocess, "run", _fake_run)
        monkeypatch.setattr(cs, "track_cost", lambda *a, **kw: 0.04)
        from pathlib import Path
        td = Path(cs.OUTPUT_DIR) / "test-scene-collision"
        td.mkdir(parents=True, exist_ok=True)
        try:
            monkeypatch.setattr(cs, "OUTPUT_DIR", td)
            r1 = cs.run_cli_composite("test", "/tmp/fake.png", "key", "1:1", tier="quality")
            r2 = cs.run_cli_composite("test", "/tmp/fake.png", "key", "1:1", tier="quality")
            assert r1 and "error" not in r1[0], f"first call failed: {r1}"
            assert r2 and "error" not in r2[0], f"second call failed: {r2}"
            name1 = r1[0].get("name", "")
            name2 = r2[0].get("name", "")
            assert name1 != name2, (
                f"Both calls produced the same filename: {name1!r} == {name2!r}. "
                f"This is the scene-set collision bug — all 5 scenes would "
                f"overwrite each other's output files."
            )
            # Each name should also contain the timestamp so the
            # user can find them by time
            assert "1700000000" in name1 and "1700000000" in name2
        finally:
            import shutil
            shutil.rmtree(td, ignore_errors=True)

    def test_run_cli_composite_name_suffix_param(self, monkeypatch):
        """Verify that the name_suffix parameter actually gets used."""
        class FakeResult:
            stdout = ""; stderr = ""; returncode = 0
        from pathlib import Path
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        def _fake_run(args, **kw):
            fname = args[-1]
            out_dir = Path(cs.OUTPUT_DIR) / today / "composite"
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / fname).write_bytes(b"fake")
            return FakeResult()
        monkeypatch.setattr(cs.subprocess, "run", _fake_run)
        monkeypatch.setattr(cs, "track_cost", lambda *a, **kw: 0.04)
        td = Path(cs.OUTPUT_DIR) / "test-scene-suffix"
        td.mkdir(parents=True, exist_ok=True)
        try:
            monkeypatch.setattr(cs, "OUTPUT_DIR", td)
            r = cs.run_cli_composite("test", "/tmp/fake.png", "key", "1:1",
                name_suffix="inhand")
            assert r and "error" not in r[0], f"failed: {r}"
            assert "inhand" in r[0]["name"], (
                f"name_suffix not used: name={r[0]['name']!r}"
            )
        finally:
            import shutil
            shutil.rmtree(td, ignore_errors=True)


# ─── Finding 3: read endpoints missing _require_api_key() ──────────────

class TestReadEndpointsRequireKey:
    """6 read endpoints were accessible to anonymous browsers:
    /api/jobs/<id>, /api/sessions, /api/session/<id>, /api/costs,
    /api/chat/<key>/history, /api/chat/<key>/reset. Each leaked other
    users' prompts, costs, and chat history. The fix: add
    _require_api_key() to each. /api/whoami is intentionally public."""

    def test_jobs_status_requires_key(self, monkeypatch):
        monkeypatch.setattr(cs, "ALLOW_SERVER_FALLBACK", False)
        monkeypatch.setattr(cs, "SERVER_API_KEY", "")
        client = cs.app.test_client()
        r = client.get("/api/jobs/nonexistent-job-id")
        assert r.status_code == 402, (
            f"/api/jobs/<id> should require auth. Got {r.status_code}: {r.get_data(as_text=True)[:200]}"
        )

    def test_sessions_list_requires_key(self, monkeypatch):
        monkeypatch.setattr(cs, "ALLOW_SERVER_FALLBACK", False)
        monkeypatch.setattr(cs, "SERVER_API_KEY", "")
        client = cs.app.test_client()
        r = client.get("/api/sessions")
        assert r.status_code == 402

    def test_session_get_requires_key(self, monkeypatch):
        monkeypatch.setattr(cs, "ALLOW_SERVER_FALLBACK", False)
        monkeypatch.setattr(cs, "SERVER_API_KEY", "")
        client = cs.app.test_client()
        r = client.get("/api/session/sess_test123")
        assert r.status_code == 402

    def test_costs_requires_key(self, monkeypatch):
        monkeypatch.setattr(cs, "ALLOW_SERVER_FALLBACK", False)
        monkeypatch.setattr(cs, "SERVER_API_KEY", "")
        client = cs.app.test_client()
        r = client.get("/api/costs")
        assert r.status_code == 402

    def test_chat_history_requires_key(self, monkeypatch):
        monkeypatch.setattr(cs, "ALLOW_SERVER_FALLBACK", False)
        monkeypatch.setattr(cs, "SERVER_API_KEY", "")
        client = cs.app.test_client()
        r = client.get("/api/chat/chat-test/history")
        assert r.status_code == 402

    def test_chat_reset_requires_key(self, monkeypatch):
        monkeypatch.setattr(cs, "ALLOW_SERVER_FALLBACK", False)
        monkeypatch.setattr(cs, "SERVER_API_KEY", "")
        client = cs.app.test_client()
        r = client.post("/api/chat/chat-test/reset")
        assert r.status_code == 402

    def test_jobs_status_with_key_works(self, monkeypatch):
        """Sanity check: with a valid key, the endpoint still works
        (returns 404 for a nonexistent job, not 402)."""
        client = cs.app.test_client()
        r = client.get("/api/jobs/nonexistent",
            headers={"X-API-Key": "AIzaTest"})
        assert r.status_code == 404

    def test_sessions_with_key_works(self, monkeypatch):
        client = cs.app.test_client()
        r = client.get("/api/sessions",
            headers={"X-API-Key": "AIzaTest"})
        assert r.status_code == 200

    def test_whoami_remains_public(self):
        """The whoami endpoint is intentionally public — it tells the
        client whether the server is in BYOK-only mode or has a
        server-fallback key. Don't accidentally lock it down."""
        client = cs.app.test_client()
        r = client.get("/api/whoami")
        assert r.status_code == 200
