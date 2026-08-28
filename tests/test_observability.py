import json
from pathlib import Path

from flask import Flask

from creative_studio_app.observability import install_request_metrics, summarize_request_metrics


def test_request_metrics_use_route_template_and_exclude_sensitive_input():
    app = Flask(__name__)
    events = []
    ticks = iter([10.0, 10.125])
    install_request_metrics(app, events.append, clock=lambda: next(ticks))

    @app.post("/api/session/<session_id>")
    def endpoint(session_id):
        return {"ok": bool(session_id)}

    response = app.test_client().post(
        "/api/session/sess_secret123?token=query-secret",
        json={"prompt": "private customer prompt"},
        headers={"Authorization": "Bearer header-secret", "X-Request-ID": "caller-123"},
    )
    event = json.loads(events[0])
    assert event == {
        "event": "http_request",
        "latency_ms": 125.0,
        "method": "POST",
        "request_id": "caller-123",
        "route": "/api/session/<session_id>",
        "schema_version": 1,
        "status": 200,
    }
    serialized = events[0]
    for secret in ("sess_secret123", "query-secret", "private customer prompt", "header-secret"):
        assert secret not in serialized
    assert response.headers["X-Request-ID"] == "caller-123"


def test_metrics_failure_never_breaks_request():
    app = Flask(__name__)
    install_request_metrics(app, lambda _event: (_ for _ in ()).throw(RuntimeError("down")))

    @app.get("/health")
    def health():
        return "ok"

    assert app.test_client().get("/health").status_code == 200


def test_provider_and_billing_errors_never_return_raw_exception_text():
    root = Path(__file__).parent.parent / "creative_studio_app"
    for name in ("generation.py", "chat.py", "iterations.py", "delivery.py", "billing_routes.py"):
        source = (root / name).read_text()
        assert "stderr[:" not in source
        assert '"message": str(error)' not in source


def test_synthetic_metrics_exercise_spend_error_latency_and_queue_alerts():
    lines = [
        json.dumps({"event": "http_request", "status": 200, "latency_ms": 100}),
        json.dumps({"event": "http_request", "status": 503, "latency_ms": 20_000}),
        json.dumps({"event": "magic_link_delivery", "success": False, "consecutive_failures": 5}),
        "malformed",
    ]
    summary = summarize_request_metrics(
        lines, spend_today=4.1, daily_limit=5.0, queue_depth=21,
        error_rate_limit=0.1, latency_p95_limit_ms=5_000, queue_depth_limit=20,
    )
    assert summary["request_count"] == 2
    assert summary["error_rate"] == 0.5
    assert summary["alerts"] == [
        "daily_spend_80_percent", "http_error_rate", "http_latency_p95", "queue_depth",
        "email_delivery_failures",
    ]
