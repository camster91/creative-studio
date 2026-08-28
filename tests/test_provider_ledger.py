import json
import time
import subprocess

from flask import Flask

from creative_studio_app.provider_ledger import ProviderLedger
from creative_studio_app.provider_metrics_routes import create_blueprint
from creative_studio_app.provider_errors import error_code


def test_ledger_is_idempotent_and_contains_no_creative_content(tmp_path):
    ledger = ProviderLedger(tmp_path / "provider.db")
    payload = dict(
        owner_id="key:hashed-owner", job_id="job_123", provider="google-gemini",
        model="gemini-test", estimated_cost=0.05, actual_cost=0.06,
        latency_ms=1200, outcome="completed", correlation_id="pcall_123",
    )
    assert ledger.record(**payload) is True
    assert ledger.record(**payload) is True
    summary = ledger.summary()
    assert summary["calls"] == 1
    assert summary["estimate_variance"] == 0.01
    serialized = json.dumps(summary)
    for secret in ("prompt", "image", "api-key", "session-token", "person@example.com"):
        assert secret not in serialized


def test_synthetic_dashboard_queries_and_alerts(tmp_path):
    ledger = ProviderLedger(tmp_path / "provider.db")
    ledger.record(
        owner_id="owner", job_id="job1", provider="google-gemini", model="m1",
        estimated_cost=0.5, actual_cost=0.5, latency_ms=100,
        outcome="completed", correlation_id="pcall_1",
    )
    ledger.record(
        owner_id="owner", job_id="job2", provider="google-gemini", model="m1",
        estimated_cost=0.5, actual_cost=0, latency_ms=6000,
        outcome="quota_exhausted", correlation_id="pcall_2",
    )
    assert ledger.alerts(
        since=time.time() - 60, spend_limit=0.6,
        error_rate_limit=0.1, latency_limit_ms=5000,
    ) == [
        "provider_spend_80_percent", "provider_error_rate",
        "provider_latency", "provider_quota_exhausted",
    ]
    app = Flask(__name__)
    app.register_blueprint(create_blueprint(
        ledger=ledger, admin_authed=lambda: True, rate_limited=lambda function: function,
    ))
    body = app.test_client().get("/api/admin/provider-metrics?hours=1").get_json()
    assert body["calls"] == 2
    assert body["groups"][1]["outcome"] == "quota_exhausted"

    locked = Flask(__name__)
    locked.register_blueprint(create_blueprint(
        ledger=ledger, admin_authed=lambda: False,
        rate_limited=lambda function: function,
    ))
    assert locked.test_client().get("/api/admin/provider-metrics").status_code == 401


def test_metrics_failure_is_non_blocking(tmp_path):
    ledger = ProviderLedger(tmp_path / "provider.db")
    ledger.path = tmp_path / "missing" / "provider.db"
    assert ledger.record(
        owner_id="owner", job_id="job", provider="provider", model="model",
        estimated_cost=0, actual_cost=0, latency_ms=0,
        outcome="completed", correlation_id="pcall_failure",
    ) is False


def test_provider_errors_classify_quota_and_timeout_without_exposing_stderr():
    quota = subprocess.CalledProcessError(
        1, "provider", stderr="private prompt RESOURCE_EXHAUSTED quota"
    )
    assert error_code(quota) == "quota_exhausted"
    assert error_code(subprocess.TimeoutExpired("provider", 300)) == "timeout"
    assert "private prompt" not in error_code(quota)
