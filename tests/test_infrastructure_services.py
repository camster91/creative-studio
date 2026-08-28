"""Focused tests for infrastructure extracted from the legacy web module."""

import threading
import time

from flask import Flask

from creative_studio_app.jobs import evict_old_jobs, job_id, run_job_background
from creative_studio_app.rate_limit import create_rate_limiter
from creative_studio_app.assets import (
    build_pin_prompt,
    image_url,
    pin_to_region,
    safe_output_relpath,
)
from creative_studio_app.costs import (
    check_daily_limit,
    load_costs,
    track_cost,
)


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


def test_output_path_resolution_and_url_are_confined(tmp_path):
    output_dir = tmp_path / "outputs"
    image = output_dir / "session" / "image.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"png")

    assert image_url(str(image), output_dir) == "/image/session/image.png"
    assert safe_output_relpath("session/image.png", output_dir) == image.resolve()
    assert safe_output_relpath("../secret", output_dir) is None


def test_spatial_pin_prompt_preserves_region_semantics():
    assert pin_to_region(0.1, 0.1) == "top-left"
    assert pin_to_region(0.5, 0.5) == "center"
    prompt = build_pin_prompt([
        {"x": 0.8, "y": 0.8, "text": "remove the glare"},
    ])
    assert "bottom-right: remove the glare" in prompt
    assert "Preserve all other areas" in prompt


def test_cost_accounting_and_limit_are_atomic(tmp_path):
    path = tmp_path / "costs.json"
    lock = threading.Lock()
    prices = {"model": {"1K": 0.25}}
    tiers = {"fast": ("model", "1K")}

    assert track_cost(path, prices, "model", "1K", 2, lock) == 0.5
    assert load_costs(path)["image_count"] == 2
    rejection = check_daily_limit(
        path,
        estimated_count=1,
        tier="fast",
        daily_limit=0.6,
        tier_models=tiers,
        price_card=prices,
        lock=lock,
    )

    assert rejection["spent_today"] == 0.5
    assert rejection["est_cost"] == 0.25
