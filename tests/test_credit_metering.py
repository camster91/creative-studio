"""
Regression tests for credit metering (fix/photogen-credit-metering).

- Polling / cancel / read endpoints never spend credits.
- /api/generate charges variations x CREDIT_WEIGHT_BY_TIER[tier],
  all-or-nothing, only after every input is validated.
- Undelivered images are refunded.

The image provider is always stubbed; nothing here touches the network.

Run:  pytest tests/test_credit_metering.py -v
"""
import importlib.util
import sys
import threading
from pathlib import Path

import pytest
from flask import Flask

from creative_studio_app.billing import CREDIT_WEIGHT_BY_TIER, credits_for
from creative_studio_app.generation_routes import create_blueprint as generation_blueprint
from creative_studio_app.jobs import DurableJobStore

SCRIPT_DIR = Path(__file__).parent.parent / "scripts"
_load_counter = [0]


def _load_module(tmp_path, monkeypatch, *, durable_jobs=False):
    """Credit mode: server key set, fallback off, provider stubbed."""
    monkeypatch.setenv("CREATIVE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GEMINI_API_KEY", "test-server-key")
    monkeypatch.setenv("CREATIVE_DURABLE_JOBS_ENABLED", "true" if durable_jobs else "false")
    monkeypatch.setenv("CREATIVE_DAILY_LIMIT", "0")
    sys.path.insert(0, str(SCRIPT_DIR))
    _load_counter[0] += 1
    name = f"creative_studio_web_metering_{_load_counter[0]}"
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / "creative-studio-web.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    mod.AUTH_DB = tmp_path / "users.db"
    mod._init_auth_schema()
    mod.SERVER_API_KEY = "test-server-key"
    mod.ALLOW_SERVER_FALLBACK = False
    mod._RATE_LIMIT = 1000
    mod.provider_calls = []

    def fake_generate(*_args, **kwargs):
        mod.provider_calls.append(kwargs.get("variations", 1))
        return [
            {"url": "/image/stub.png", "cost": 0.02, "model": "stub"}
            for _ in range(kwargs.get("variations", 1))
        ]

    mod.run_cli_generate = fake_generate
    with mod._request_log_lock:
        mod._request_log.clear()
    return mod


@pytest.fixture
def cs(tmp_path, monkeypatch):
    return _load_module(tmp_path, monkeypatch)


def _login(mod, email, credits=None):
    client = mod.app.test_client()
    token = client.post("/signup", json={"email": email}).get_json()["token"]
    session = client.post("/login", json={"token": token}).get_json()["session_token"]
    if credits is not None:
        with mod._auth_db() as db:
            db.execute("UPDATE users SET credits_remaining = ? WHERE email = ?", (credits, email))
            db.commit()
    return client, {"X-Session-Token": session}


def _balance(mod, email):
    with mod._auth_db() as db:
        return db.execute(
            "SELECT credits_remaining FROM users WHERE email = ?", (email,)
        ).fetchone()["credits_remaining"]


# ─── Weight table ────────────────────────────────────────────────────

class TestWeightTable:
    def test_tiers_match_generator_tiers(self, cs):
        assert set(CREDIT_WEIGHT_BY_TIER) == set(cs._TIER_MODEL)

    def test_higher_tiers_cost_more(self):
        w = CREDIT_WEIGHT_BY_TIER
        assert w["fast"] <= w["balanced"] < w["quality"] < w["ultra"]
        assert credits_for("ultra", 8) == 8 * w["ultra"]


# ─── Read-only endpoints never spend ─────────────────────────────────

class TestPollingIsFree:
    def test_job_status_polling_does_not_charge(self, cs):
        client, headers = _login(cs, "poll@x.co")
        for _ in range(10):
            r = client.get("/api/jobs/nonexistent-job", headers=headers)
            assert r.status_code == 404
        assert _balance(cs, "poll@x.co") == 5

    def test_cancel_does_not_charge(self, cs):
        client, headers = _login(cs, "cancel@x.co")
        r = client.post("/api/jobs/nonexistent-job/cancel", headers=headers)
        assert r.status_code == 404
        assert _balance(cs, "cancel@x.co") == 5

    def test_zero_balance_user_can_still_poll(self, cs):
        client, headers = _login(cs, "broke@x.co", credits=0)
        r = client.get("/api/jobs/nonexistent-job", headers=headers)
        assert r.status_code == 404  # not 402

    def test_state_and_chat_reads_do_not_charge(self, cs):
        client, headers = _login(cs, "reader@x.co")
        assert client.get("/api/sessions", headers=headers).status_code == 200
        assert client.get("/api/costs", headers=headers).status_code == 200
        client.get("/api/chat/chat-00000000/history", headers=headers)
        client.post("/api/chat/chat-00000000/reset", headers=headers)
        assert _balance(cs, "reader@x.co") == 5

    def test_anonymous_polling_still_rejected(self, cs):
        r = cs.app.test_client().get("/api/jobs/nonexistent-job")
        assert r.status_code == 402


# ─── /api/generate charges variations x weight ───────────────────────

class TestGenerateCharge:
    @pytest.mark.parametrize("tier,variations", [
        ("fast", 1), ("balanced", 3), ("quality", 4), ("ultra", 2),
    ])
    def test_charge_is_variations_times_weight(self, cs, tier, variations):
        client, headers = _login(cs, f"{tier}@x.co", credits=40)
        r = client.post("/api/generate", headers=headers, json={
            "prompt": "x", "tier": tier, "variations": variations, "aspect_ratio": "1:1",
        })
        assert r.status_code == 200, r.get_json()
        expected = variations * CREDIT_WEIGHT_BY_TIER[tier]
        assert r.get_json()["credits_charged"] == expected
        assert _balance(cs, f"{tier}@x.co") == 40 - expected

    @pytest.mark.parametrize("variations", [0, 9, -1, "many", None, True])
    def test_invalid_variations_is_400_and_free(self, cs, variations):
        client, headers = _login(cs, "badvar@x.co")
        r = client.post("/api/generate", headers=headers, json={
            "prompt": "x", "tier": "fast", "variations": variations,
        })
        assert r.status_code == 400
        assert "variations" in r.get_json()["error"]
        assert _balance(cs, "badvar@x.co") == 5
        assert cs.provider_calls == []

    def test_unknown_tier_is_400_and_free(self, cs):
        client, headers = _login(cs, "badtier@x.co")
        r = client.post("/api/generate", headers=headers, json={
            "prompt": "x", "tier": "mega", "variations": 1,
        })
        assert r.status_code == 400
        assert _balance(cs, "badtier@x.co") == 5

    def test_missing_prompt_is_free(self, cs):
        client, headers = _login(cs, "noprompt@x.co")
        r = client.post("/api/generate", headers=headers, json={"tier": "fast"})
        assert r.status_code == 400
        assert _balance(cs, "noprompt@x.co") == 5

    def test_insufficient_credits_is_402_and_spends_nothing(self, cs):
        client, headers = _login(cs, "short@x.co", credits=3)
        r = client.post("/api/generate", headers=headers, json={
            "prompt": "x", "tier": "ultra", "variations": 1,
        })
        assert r.status_code == 402
        body = r.get_json()
        assert body["credits_required"] == CREDIT_WEIGHT_BY_TIER["ultra"]
        assert body["credits_remaining"] == 3
        assert _balance(cs, "short@x.co") == 3
        assert cs.provider_calls == []

    def test_eight_variations_no_longer_cost_one_credit(self, cs):
        client, headers = _login(cs, "eight@x.co")  # 5 trial credits
        r = client.post("/api/generate", headers=headers, json={
            "prompt": "x", "tier": "fast", "variations": 8,
        })
        assert r.status_code == 402
        assert _balance(cs, "eight@x.co") == 5

    def test_failed_images_are_refunded(self, cs):
        cs.run_cli_generate = lambda *_a, **kw: [
            {"url": "/image/ok.png", "cost": 0.09, "model": "stub"},
            {"error": "provider failed", "error_code": "provider_failed"},
        ]
        client, headers = _login(cs, "refund@x.co", credits=10)
        r = client.post("/api/generate", headers=headers, json={
            "prompt": "x", "tier": "quality", "variations": 2,
        })
        assert r.status_code == 200
        weight = CREDIT_WEIGHT_BY_TIER["quality"]
        assert r.get_json()["credits_charged"] == weight
        assert _balance(cs, "refund@x.co") == 10 - weight

    def test_byok_never_charges(self, cs):
        client, headers = _login(cs, "byok@x.co")
        headers["X-API-Key"] = "AIzaOwnKey"
        r = client.post("/api/generate", headers=headers, json={
            "prompt": "x", "tier": "ultra", "variations": 4,
        })
        assert r.status_code == 200
        assert _balance(cs, "byok@x.co") == 5


# ─── Batch (durable job) path, via the blueprint directly ────────────

def _batch_app(tmp_path, *, charges, refunds, estimate=0.05, provider=None):
    app = Flask(__name__)
    store = DurableJobStore(tmp_path / "jobs.db")
    jobs = {}

    def require_api_key(credits=1):
        charges.append(credits)
        return "key", None, True

    def run_background(identifier, function):
        store.update(identifier, status="running")
        jobs[identifier] = {"status": "running", "result": None}
        result = function()
        store.update(identifier, status=result.pop("_job_status", "completed"), result=result)

    app.register_blueprint(generation_blueprint(
        enforce_prompt_length=lambda _prompt: None,
        require_api_key=require_api_key,
        enforce_daily_limit=lambda *_args: None,
        parse_figma_url=lambda _url: (None, None),
        fetch_figma_context=lambda *_args: {},
        enhance_prompt_with_figma=lambda prompt, _context: prompt,
        new_session_id=lambda: "sess_deadbeef",
        new_job_id=lambda: "fallback-key",
        run_job_background=run_background,
        jobs=jobs,
        jobs_lock=threading.Lock(),
        run_generate=provider or (lambda *_a, **_k: [{"url": "/image/1.png", "cost": 0.05}]),
        add_entry=lambda *_args: None,
        load_costs=lambda: {},
        save_costs=lambda _costs: None,
        get_sessions_dir=lambda: tmp_path,
        current_session=lambda: {"user_id": "u1", "credits_remaining": 0},
        current_actor_id=lambda: "user:u1",
        job_store=store,
        estimate_cost=lambda _tier: estimate,
        max_job_cost=1.0,
        rate_limited=lambda function: function,
        refund_credits=lambda user_id, amount: refunds.append((user_id, amount)),
    ))
    return app.test_client()


class TestBatchCharge:
    def test_batch_charges_variations_times_weight(self, tmp_path):
        charges, refunds = [], []
        client = _batch_app(tmp_path, charges=charges, refunds=refunds)
        r = client.post("/api/generate", json={"prompt": "x", "tier": "quality", "variations": 3})
        assert r.status_code == 200
        assert charges == [3 * CREDIT_WEIGHT_BY_TIER["quality"]]
        assert refunds == []

    def test_idempotent_replay_is_refunded(self, tmp_path):
        charges, refunds = [], []
        client = _batch_app(tmp_path, charges=charges, refunds=refunds)
        payload = {"prompt": "x", "tier": "balanced", "variations": 2}
        client.post("/api/generate", json=payload, headers={"Idempotency-Key": "k1"})
        r = client.post("/api/generate", json=payload, headers={"Idempotency-Key": "k1"})
        assert r.get_json()["idempotent_replay"] is True
        assert refunds == [("u1", 2 * CREDIT_WEIGHT_BY_TIER["balanced"])]

    def test_over_job_budget_rejected_before_charge(self, tmp_path):
        charges, refunds = [], []
        client = _batch_app(tmp_path, charges=charges, refunds=refunds, estimate=0.6)
        r = client.post("/api/generate", json={"prompt": "x", "variations": 2})
        assert r.status_code == 400
        assert charges == []

    def test_invalid_idempotency_key_rejected_before_charge(self, tmp_path):
        charges, refunds = [], []
        client = _batch_app(tmp_path, charges=charges, refunds=refunds)
        r = client.post("/api/generate", json={"prompt": "x", "variations": 2},
                        headers={"Idempotency-Key": "k" * 200})
        assert r.status_code == 400
        assert charges == []

    def test_provider_failure_refunds_undelivered_images(self, tmp_path):
        charges, refunds = [], []
        calls = []

        def provider(*_a, **_k):
            calls.append(1)
            if len(calls) == 1:
                return [{"url": "/image/1.png", "cost": 0.24}]
            return [{"error": "boom", "error_code": "provider_failed"}]

        client = _batch_app(tmp_path, charges=charges, refunds=refunds, provider=provider)
        r = client.post("/api/generate", json={"prompt": "x", "tier": "ultra", "variations": 4})
        assert r.status_code == 200
        assert charges == [4 * CREDIT_WEIGHT_BY_TIER["ultra"]]
        assert refunds == [("u1", 3 * CREDIT_WEIGHT_BY_TIER["ultra"])]
