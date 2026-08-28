import threading
from pathlib import Path

import pytest
from flask import Flask

from creative_studio_app.generation_routes import create_blueprint as generation_blueprint
from creative_studio_app.jobs import DurableJobStore


def test_idempotency_is_owner_scoped_and_payload_bound(tmp_path):
    store = DurableJobStore(tmp_path / "jobs.db")
    first, created = store.create_or_get("user:a", "same-key", "fingerprint-a", 0.2)
    replay, replay_created = store.create_or_get("user:a", "same-key", "fingerprint-a", 0.2)
    other_owner, other_created = store.create_or_get("user:b", "same-key", "fingerprint-a", 0.2)
    assert created is True and replay_created is False
    assert replay["id"] == first["id"]
    assert other_created is True and other_owner["id"] != first["id"]
    with pytest.raises(ValueError, match="different request"):
        store.create_or_get("user:a", "same-key", "fingerprint-b", 0.2)


def test_partial_results_cost_and_restart_recovery_are_durable(tmp_path):
    database = tmp_path / "jobs.db"
    store = DurableJobStore(database)
    job, _ = store.create_or_get("user:a", "key", "fingerprint", 0.4)
    store.update(job["id"], status="running", result={"images": [{"url": "/one"}]}, actual_cost=0.1)
    assert DurableJobStore(database).recover_interrupted() == 1
    recovered = DurableJobStore(database).get(job["id"], "user:a")
    assert recovered["status"] == "failed"
    assert recovered["error_code"] == "service_restarted"
    assert recovered["result"]["images"] == [{"url": "/one"}]
    assert recovered["actual_cost"] == 0.1


def test_cancel_is_owner_scoped_and_terminal_for_queued_job(tmp_path):
    store = DurableJobStore(tmp_path / "jobs.db")
    job, _ = store.create_or_get("user:a", "key", "fingerprint", 0.4)
    assert store.request_cancel(job["id"], "user:b") is None
    cancelled = store.request_cancel(job["id"], "user:a")
    assert cancelled["status"] == "cancelled"
    assert store.cancellation_requested(job["id"]) is True


def test_concurrent_duplicate_creation_produces_one_job(tmp_path):
    store = DurableJobStore(tmp_path / "jobs.db")
    results = []
    lock = threading.Lock()

    def create():
        result = store.create_or_get("user:a", "key", "fingerprint", 0.4)
        with lock:
            results.append(result)

    threads = [threading.Thread(target=create) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len({item[0]["id"] for item in results}) == 1
    assert sum(item[1] for item in results) == 1


def test_batch_endpoint_replay_does_not_repeat_provider_calls(tmp_path):
    app = Flask(__name__)
    store = DurableJobStore(tmp_path / "jobs.db")
    jobs = {}
    jobs_lock = threading.Lock()
    provider_calls = []

    def run_generate(*_args, **_kwargs):
        provider_calls.append(1)
        return [{"url": f"/image/{len(provider_calls)}.png", "cost": 0.05, "model": "test"}]

    def run_background(identifier, function):
        store.update(identifier, status="running")
        jobs[identifier] = {"status": "running", "result": None}
        result = function()
        store.update(identifier, status=result.pop("_job_status", "completed"), result=result, actual_cost=sum(i["cost"] for i in result["images"]))

    app.register_blueprint(generation_blueprint(
        enforce_prompt_length=lambda _prompt: None,
        require_api_key=lambda: ("key", None, False),
        enforce_daily_limit=lambda _count, _tier: None,
        parse_figma_url=lambda _url: (None, None),
        fetch_figma_context=lambda *_args: {},
        enhance_prompt_with_figma=lambda prompt, _context: prompt,
        new_session_id=lambda: "sess_deadbeef",
        new_job_id=lambda: "generated-fallback-key",
        run_job_background=run_background,
        jobs=jobs,
        jobs_lock=jobs_lock,
        run_generate=run_generate,
        add_entry=lambda *_args: None,
        load_costs=lambda: {},
        save_costs=lambda _costs: None,
        get_sessions_dir=lambda: tmp_path,
        current_session=lambda: None,
        current_actor_id=lambda: "key:owner",
        job_store=store,
        estimate_cost=lambda _tier: 0.05,
        max_job_cost=1.0,
        rate_limited=lambda function: function,
    ))
    client = app.test_client()
    payload = {"prompt": "same", "variations": 2, "session_id": "sess_deadbeef"}
    first = client.post("/api/generate", json=payload, headers={"Idempotency-Key": "request-1"})
    second = client.post("/api/generate", json=payload, headers={"Idempotency-Key": "request-1"})
    assert first.status_code == 200 and second.status_code == 200
    assert first.get_json()["job_id"] == second.get_json()["job_id"]
    assert second.get_json()["idempotent_replay"] is True
    assert len(provider_calls) == 2


def test_per_job_budget_rejects_before_provider_or_job_creation(tmp_path):
    app = Flask(__name__)
    store = DurableJobStore(tmp_path / "jobs.db")
    calls = []
    app.register_blueprint(generation_blueprint(
        enforce_prompt_length=lambda _prompt: None, require_api_key=lambda: ("key", None, False),
        enforce_daily_limit=lambda *_args: None, parse_figma_url=lambda _url: (None, None),
        fetch_figma_context=lambda *_args: {}, enhance_prompt_with_figma=lambda p, _c: p,
        new_session_id=lambda: "sess_deadbeef", new_job_id=lambda: "fallback",
        run_job_background=lambda *_args: calls.append("background"), jobs={}, jobs_lock=threading.Lock(),
        run_generate=lambda *_args, **_kwargs: calls.append("provider"), add_entry=lambda *_args: None,
        load_costs=lambda: {}, save_costs=lambda _data: None, get_sessions_dir=lambda: tmp_path,
        current_session=lambda: None, current_actor_id=lambda: "key:owner", job_store=store,
        estimate_cost=lambda _tier: 0.6, max_job_cost=1.0, rate_limited=lambda f: f,
    ))
    response = app.test_client().post("/api/generate", json={"prompt": "x", "variations": 2})
    assert response.status_code == 400
    assert response.get_json()["estimated_cost"] == 1.2
    assert calls == []
