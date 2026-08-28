"""Bounded, in-memory request rate limiting for the Flask application."""

import os
import time
from functools import wraps
from typing import Callable

from flask import jsonify, request


def client_ip() -> str:
    """Return the peer IP, optionally trusting the proxy's final XFF value."""
    trust_proxy = os.environ.get("TRUST_PROXY", "").lower() in ("1", "true", "yes")
    if trust_proxy:
        forwarded_for = request.headers.get("X-Forwarded-For", "")
        if forwarded_for:
            final_hop = forwarded_for.split(",")[-1].strip()
            if final_hop:
                return final_hop
    return request.remote_addr or "unknown"


def create_rate_limiter(
    get_limit: Callable[[], int],
    get_max_tracked_ips: Callable[[], int],
    request_log: dict[str, list[float]],
    request_log_lock,
):
    """Build a decorator whose limits remain configurable by the app module."""

    def rate_limited(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            ip = client_ip()
            now = time.time()
            with request_log_lock:
                request_log.setdefault(ip, [])
                request_log[ip] = [stamp for stamp in request_log[ip] if now - stamp < 60]
                for stale_ip in [key for key, stamps in request_log.items() if not stamps]:
                    if stale_ip != ip:
                        request_log.pop(stale_ip, None)
                if len(request_log) > get_max_tracked_ips():
                    oldest_ip = min(
                        request_log,
                        key=lambda key: request_log[key][-1] if request_log[key] else 0,
                    )
                    if oldest_ip != ip:
                        request_log.pop(oldest_ip, None)
                if len(request_log[ip]) >= get_limit():
                    return jsonify({"error": "Rate limit exceeded. Try again later."}), 429
                request_log[ip].append(now)
            return func(*args, **kwargs)

        return wrapper

    return rate_limited
