"""Privacy-safe provider call ledger and operator summaries."""

from __future__ import annotations

import sqlite3
import time
import uuid
import math
from pathlib import Path


LEDGER_SCHEMA_VERSION = 1
OUTCOMES = {"completed", "partial", "provider_failed", "quota_exhausted", "timeout", "service_unavailable"}


def correlation_id() -> str:
    return "pcall_" + uuid.uuid4().hex[:20]


def _nonnegative(value) -> float:
    parsed = float(value)
    return parsed if math.isfinite(parsed) and parsed >= 0 else 0.0


class ProviderLedger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as database:
            database.executescript(
                """
                CREATE TABLE IF NOT EXISTS provider_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    schema_version INTEGER NOT NULL,
                    owner_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    estimated_cost REAL NOT NULL,
                    actual_cost REAL NOT NULL,
                    latency_ms REAL NOT NULL,
                    outcome TEXT NOT NULL,
                    correlation_id TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS provider_calls_created ON provider_calls(created_at);
                CREATE INDEX IF NOT EXISTS provider_calls_owner ON provider_calls(owner_id,created_at);
                """
            )

    def _connect(self):
        database = sqlite3.connect(self.path, timeout=10)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA journal_mode=WAL")
        return database

    def record(
        self,
        *,
        owner_id: str,
        job_id: str,
        provider: str,
        model: str,
        estimated_cost: float,
        actual_cost: float,
        latency_ms: float,
        outcome: str,
        correlation_id: str,
    ) -> bool:
        """Best-effort idempotent write; telemetry can never break a request."""
        try:
            if not owner_id or not job_id or not correlation_id or outcome not in OUTCOMES:
                return False
            with self._connect() as database:
                database.execute(
                    """INSERT OR IGNORE INTO provider_calls
                       (schema_version,owner_id,job_id,provider,model,estimated_cost,
                        actual_cost,latency_ms,outcome,correlation_id,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        LEDGER_SCHEMA_VERSION, str(owner_id)[:160], str(job_id)[:160],
                        str(provider)[:80], str(model)[:200], _nonnegative(estimated_cost),
                        _nonnegative(actual_cost), _nonnegative(latency_ms), outcome,
                        str(correlation_id)[:100], time.time(),
                    ),
                )
                database.commit()
            return True
        except Exception:
            return False

    def summary(self, *, since: float = 0, owner_id: str | None = None) -> dict:
        where = ["created_at>=?"]
        values: list = [max(0, float(since))]
        if owner_id:
            where.append("owner_id=?")
            values.append(owner_id)
        clause = " AND ".join(where)
        with self._connect() as database:
            totals = database.execute(
                f"""SELECT COUNT(*) calls,COALESCE(SUM(estimated_cost),0) estimated,
                    COALESCE(SUM(actual_cost),0) actual,
                    COALESCE(AVG(latency_ms),0) latency_avg,
                    COALESCE(MAX(latency_ms),0) latency_max
                    FROM provider_calls WHERE {clause}""",
                values,
            ).fetchone()
            groups = database.execute(
                f"""SELECT provider,model,outcome,COUNT(*) calls,
                    SUM(estimated_cost) estimated,SUM(actual_cost) actual,
                    AVG(latency_ms) latency_avg
                    FROM provider_calls WHERE {clause}
                    GROUP BY provider,model,outcome ORDER BY provider,model,outcome""",
                values,
            ).fetchall()
        actual = float(totals["actual"])
        estimated = float(totals["estimated"])
        return {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "calls": totals["calls"],
            "estimated_cost": round(estimated, 6),
            "actual_cost": round(actual, 6),
            "estimate_variance": round(actual - estimated, 6),
            "latency_avg_ms": round(float(totals["latency_avg"]), 2),
            "latency_max_ms": round(float(totals["latency_max"]), 2),
            "groups": [
                {
                    "provider": row["provider"], "model": row["model"],
                    "outcome": row["outcome"], "calls": row["calls"],
                    "estimated_cost": round(row["estimated"], 6),
                    "actual_cost": round(row["actual"], 6),
                    "latency_avg_ms": round(row["latency_avg"], 2),
                }
                for row in groups
            ],
        }

    def alerts(
        self,
        *,
        since: float,
        spend_limit: float,
        error_rate_limit: float,
        latency_limit_ms: float,
    ) -> list[str]:
        with self._connect() as database:
            rows = database.execute(
                "SELECT outcome,actual_cost,latency_ms FROM provider_calls WHERE created_at>=?",
                (since,),
            ).fetchall()
        if not rows:
            return []
        alerts = []
        if spend_limit > 0 and sum(row["actual_cost"] for row in rows) >= spend_limit * 0.8:
            alerts.append("provider_spend_80_percent")
        errors = sum(row["outcome"] != "completed" for row in rows)
        if errors / len(rows) > error_rate_limit:
            alerts.append("provider_error_rate")
        if max(row["latency_ms"] for row in rows) > latency_limit_ms:
            alerts.append("provider_latency")
        if any(row["outcome"] == "quota_exhausted" for row in rows):
            alerts.append("provider_quota_exhausted")
        return alerts
