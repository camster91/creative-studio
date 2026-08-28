"""Privacy-safe request correlation and structured access metrics."""

import json
import time
import uuid
from statistics import quantiles

from flask import g, request


def install_request_metrics(app, emit, *, clock=time.monotonic) -> None:
    """Emit route-template metrics without headers, bodies, queries, or IDs."""

    @app.before_request
    def begin_request_metrics():
        supplied = request.headers.get("X-Request-ID", "")
        g.request_id = supplied if supplied.isascii() and supplied.replace("-", "").isalnum() and len(supplied) <= 64 else uuid.uuid4().hex
        g.request_started = clock()

    @app.after_request
    def finish_request_metrics(response):
        try:
            rule = request.url_rule.rule if request.url_rule else "unmatched"
            started = getattr(g, "request_started", None)
            finished = clock()
            latency_ms = round(max(0, finished - started) * 1000, 2) if started is not None else None
            event = {
                "schema_version": 1,
                "event": "http_request",
                "request_id": g.get("request_id", uuid.uuid4().hex),
                "method": request.method,
                "route": rule,
                "status": response.status_code,
                "latency_ms": latency_ms,
            }
            emit(json.dumps(event, separators=(",", ":"), sort_keys=True))
        except Exception:
            pass
        response.headers["X-Request-ID"] = g.get("request_id", uuid.uuid4().hex)
        return response


def summarize_request_metrics(lines, *, spend_today=0.0, daily_limit=0.0, queue_depth=0, error_rate_limit=0.05, latency_p95_limit_ms=10_000, queue_depth_limit=20):
    """Summarize synthetic/live JSONL and return deterministic alert reasons."""
    events = []
    delivery_failures = 0
    for line in lines:
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if event.get("event") == "http_request" and isinstance(event.get("status"), int):
            events.append(event)
        elif event.get("event") == "magic_link_delivery" and event.get("success") is False:
            delivery_failures = max(
                delivery_failures, int(event.get("consecutive_failures") or 1)
            )
    request_count = len(events)
    error_count = sum(event["status"] >= 500 for event in events)
    error_rate = error_count / request_count if request_count else 0.0
    latencies = sorted(float(event["latency_ms"]) for event in events if isinstance(event.get("latency_ms"), (int, float)))
    p95 = quantiles(latencies, n=100, method="inclusive")[94] if len(latencies) >= 2 else latencies[0] if latencies else 0.0
    alerts = []
    if daily_limit > 0 and spend_today >= daily_limit * 0.8:
        alerts.append("daily_spend_80_percent")
    if error_rate > error_rate_limit:
        alerts.append("http_error_rate")
    if p95 > latency_p95_limit_ms:
        alerts.append("http_latency_p95")
    if queue_depth > queue_depth_limit:
        alerts.append("queue_depth")
    if delivery_failures >= 5:
        alerts.append("email_delivery_failures")
    return {
        "schema_version": 1,
        "request_count": request_count,
        "error_count": error_count,
        "error_rate": round(error_rate, 6),
        "latency_p95_ms": round(p95, 2),
        "spend_today": spend_today,
        "daily_limit": daily_limit,
        "queue_depth": queue_depth,
        "email_consecutive_failures": delivery_failures,
        "alerts": alerts,
    }
