"""Focused tests for infrastructure extracted from the legacy web module."""

import threading
import time

from flask import Flask

from creative_studio_app.jobs import evict_old_jobs, job_id, run_job_background
from creative_studio_app.rate_limit import create_rate_limiter


def test_job_ids_are_unique_and_prefixed():
    identifiers = {job_id() for _ in range(20)}
    assert len(identifiers) == 20
    assert all(identifier.startswith("job_") for identifier in identifiers)


def test_job_eviction_preserves_running_work():
    now = time.time()
    jobs = {
        "stale": {"status": "done", "finished_at": now - 120},
        "older": {"status": "done", "finished_at": now - 2},
        "newer": {"status": "done", "finished_at": now - 1},
        "running": {"status": "running", "started_at": now - 1000},
    }

    evict_old_jobs(jobs, max_jobs=2, ttl_seconds=60, now=now)

    assert set(jobs) == {"newer", "running"}


def test_background_job_records_success():
    jobs = {}
    lock = threading.Lock()
    completed = threading.Event()

    def work(value):
        completed.set()
        return value * 2

    run_job_background(
        "job_test",
        work,
        4,
        jobs=jobs,
        lock=lock,
        evict=lambda: None,
    )
    assert completed.wait(timeout=2)
    for _ in range(100):
        if jobs["job_test"]["status"] == "done":
            break
        time.sleep(0.01)

    assert jobs["job_test"]["status"] == "done"
    assert jobs["job_test"]["result"] == 8


def test_rate_limiter_isolated_state_and_limit():
    app = Flask(__name__)
    request_log = {}
    lock = threading.Lock()
    rate_limited = create_rate_limiter(
        lambda: 2,
        lambda: 100,
        request_log,
        lock,
    )

    @app.get("/limited")
    @rate_limited
    def limited():
        return {"ok": True}

    client = app.test_client()
    assert client.get("/limited").status_code == 200
    assert client.get("/limited").status_code == 200
    assert client.get("/limited").status_code == 429
