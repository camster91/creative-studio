"""Operator-only provider accounting queries."""

import time
import math
from collections.abc import Callable

from flask import Blueprint, jsonify, request


def create_blueprint(*, ledger, admin_authed: Callable[[], bool], rate_limited: Callable) -> Blueprint:
    blueprint = Blueprint("provider_metrics", __name__)

    @blueprint.get("/api/admin/provider-metrics")
    @rate_limited
    def provider_metrics():
        if not admin_authed():
            return jsonify({"error": "Admin authentication required"}), 401
        try:
            hours = min(24 * 90, max(1, int(request.args.get("hours", "24"))))
        except ValueError:
            return jsonify({"error": "hours must be an integer"}), 400
        try:
            spend_limit = float(request.args.get("spend_limit", "5"))
            error_rate_limit = float(request.args.get("error_rate_limit", "0.05"))
            latency_limit_ms = float(request.args.get("latency_limit_ms", "300000"))
        except ValueError:
            return jsonify({"error": "Alert limits must be numeric"}), 400
        if not all(math.isfinite(value) and value >= 0 for value in (
            spend_limit, error_rate_limit, latency_limit_ms
        )):
            return jsonify({"error": "Alert limits must be finite and non-negative"}), 400
        since = time.time() - hours * 3600
        summary = ledger.summary(since=since)
        summary["window_hours"] = hours
        summary["alerts"] = ledger.alerts(
            since=since,
            spend_limit=spend_limit,
            error_rate_limit=error_rate_limit,
            latency_limit_ms=latency_limit_ms,
        )
        return jsonify(summary)

    return blueprint
