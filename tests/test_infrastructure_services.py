"""Focused tests for infrastructure extracted from the legacy web module."""

import threading
import time
from pathlib import Path

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
from creative_studio_app.seo import markdown_to_html, parse_blog_post
from creative_studio_app.informational import render_docs, render_history, render_status
from creative_studio_app import auth as auth_service
from creative_studio_app import projects as project_service
from creative_studio_app import generation as generation_service
from creative_studio_app.delivery import parse_qc_output
from creative_studio_app import chat as chat_service
from creative_studio_app import billing as billing_service


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


def test_seo_parser_sanitizes_blog_content(tmp_path):
    post_path = tmp_path / "safe-post.md"
    post_path.write_text(
        "---\ntitle: Safe Post\ndate: 2026-08-28\n---\n"
        "## Heading\n\n<script>alert(1)</script>"
    )

    post = parse_blog_post(post_path)

    assert post["title"] == "Safe Post"
    assert "<h2>Heading</h2>" in post["body_html"]
    assert "<script>" not in post["body_html"]
    assert "&lt;script&gt;" in markdown_to_html("<script>")


def test_informational_pages_cover_operations_and_escape_history(tmp_path):
    status = render_status(
        {"total": 1.25, "by_date": {}, "image_count": 3},
        {"job_123": {"status": "running", "started_at": time.time()}},
    )
    docs = render_docs()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "one.json").write_text(
        '{"entries":[{"prompt":"<script>bad</script>","cost":0.1,"model":"test"}]}'
    )
    history = render_history(sessions, lambda path: __import__("json").loads(path.read_text()))

    assert "Active jobs (1)" in status
    assert "/api/generate" in docs and "X-API-Key" in docs
    assert "<script>bad</script>" not in history
    assert "&lt;script&gt;bad&lt;/script&gt;" in history


def test_legacy_informational_routes_remain_available():
    # The legacy module is exercised exhaustively elsewhere; this source-level
    # contract catches accidental route loss during further controller splits.
    source = (Path(__file__).parent.parent / "scripts" / "creative-studio-web.py").read_text()
    for route in ('/status', '/docs', '/history'):
        assert f'@app.route("{route}")' in source


def test_magic_link_is_single_use_under_concurrency(tmp_path):
    database = tmp_path / "users.db"
    auth_service.init_schema(database)
    token = auth_service.create_magic_link(database, "person@example.com", 60)
    results = []
    result_lock = threading.Lock()

    def consume():
        result = auth_service.consume_magic_link(
            database, token, session_days=7, free_trial_credits=2
        )
        with result_lock:
            results.append(result)

    threads = [threading.Thread(target=consume) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(result is not None for result in results) == 1


def test_trial_credits_cannot_be_overspent_concurrently(tmp_path):
    database = tmp_path / "users.db"
    auth_service.init_schema(database)
    token = auth_service.create_magic_link(database, "person@example.com", 60)
    session = auth_service.consume_magic_link(
        database, token, session_days=7, free_trial_credits=2
    )
    results = []
    threads = [
        threading.Thread(
            target=lambda: results.append(
                auth_service.use_trial_credit(database, session["user_id"])
            )
        )
        for _ in range(5)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(ok for ok, _ in results) == 2
    assert auth_service.use_trial_credit(database, session["user_id"]) == (False, 0)


def test_project_service_enforces_ownership(tmp_path):
    database = tmp_path / "users.db"
    auth_service.init_schema(database)
    with auth_service.connect(database) as connection:
        connection.execute(
            "INSERT INTO users (id,email,created_at) VALUES ('owner','o@example.com','now')"
        )
        connection.commit()
    project = project_service.create(
        database, "owner", "Campaign", None, max_projects=10, name_max=200
    )
    updated = project_service.add_generation(
        database,
        project["id"],
        "owner",
        url="/image/one.png",
        prompt="prompt",
        cost=0.1,
        model="model",
        ratio="1:1",
        url_max=2000,
        prompt_max=4000,
    )

    assert updated["generations"][0]["url"] == "/image/one.png"
    assert project_service.get(database, project["id"], "intruder") is None
    assert project_service.delete(database, project["id"], "intruder") is False


def test_generation_service_builds_exact_passthrough_command(tmp_path):
    from datetime import datetime
    output_dir = tmp_path / "outputs"
    calls = []

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        destination = output_dir / datetime.now().strftime("%Y-%m-%d") / "direct"
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "result.png").write_bytes(b"png")

    images = generation_service.generate(
        "literal prompt",
        "direct",
        "secret-key",
        "balanced",
        "1:1",
        False,
        input_image=None,
        variations=1,
        output_dir=output_dir,
        script_path="creative_studio.py",
        python_executable="python",
        tier_models={"balanced": ("model", "1K")},
        run=run,
        record_cost=lambda model, resolution: 0.25,
        to_image_url=lambda path: "/image/result.png",
    )

    arguments, options = calls[0]
    assert arguments[arguments.index("--prompt") + 1] == "literal prompt"
    assert options["env"]["GEMINI_API_KEY"] == "secret-key"
    assert images[0]["cost"] == 0.25


def test_qc_parser_maps_score_failures_and_warnings():
    result = parse_qc_output(
        "QC SCORE: 7/10\nFloating: FAIL\nLabels: PASS\n⚠ glare on label"
    )

    assert result["quality_score"] == 7
    assert result["floating_products"] is True
    assert result["readable_labels"] is True
    assert result["issues"] == ["glare on label"]


def test_chat_service_feeds_each_output_into_the_next_turn(tmp_path):
    sessions = {}
    calls = []

    def run(arguments, **kwargs):
        calls.append(arguments)
        output = Path(arguments[arguments.index("--filename") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"png")

    dependencies = {
        "output_dir": tmp_path,
        "script_path": "creative_studio.py",
        "python_executable": "python",
        "tier_models": {"balanced": ("model", "1K")},
        "run": run,
        "record_cost": lambda model, resolution: 0.1,
        "to_image_url": lambda path: "/image/" + Path(path).name,
    }
    first, _ = chat_service.turn(
        sessions, "chat-1", "key", "first", "balanced", "1:1",
        input_image=None, **dependencies,
    )
    second, session = chat_service.turn(
        sessions, "chat-1", "key", "second", "balanced", "1:1",
        input_image=None, **dependencies,
    )

    assert first[0]["turn"] == 1 and second[0]["turn"] == 2
    assert calls[1][calls[1].index("--input-image") + 1] == first[0]["path"]
    assert len(session["history"]) == 2


def test_billing_credit_top_up_is_retry_idempotent(tmp_path):
    database = tmp_path / "users.db"
    auth_service.init_schema(database)
    with auth_service.connect(database) as connection:
        connection.execute(
            """INSERT INTO users
               (id,email,created_at,credits_remaining,subscription_tier,subscription_status)
               VALUES ('user','paid@example.com','now',3,'pro','active')"""
        )
        connection.commit()
    plans = {"pro": {"monthly_credits": 500}}

    billing_service.top_up_credits(database, "user", plans)
    billing_service.top_up_credits(database, "user", plans)

    with auth_service.connect(database) as connection:
        user = connection.execute(
            "SELECT credits_remaining,subscription_status FROM users WHERE id='user'"
        ).fetchone()
    assert user["credits_remaining"] == 500
    assert user["subscription_status"] == "active"
