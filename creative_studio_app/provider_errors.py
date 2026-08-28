"""Stable privacy-safe provider failure classification."""

import subprocess


def error_code(error: BaseException) -> str:
    if isinstance(error, subprocess.TimeoutExpired):
        return "timeout"
    raw = getattr(error, "stderr", "") or ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "ignore")
    lowered = str(raw).lower()
    if any(marker in lowered for marker in ("quota", "resource_exhausted", "rate limit", "429")):
        return "quota_exhausted"
    return "provider_failed"
