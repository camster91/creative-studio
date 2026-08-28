"""Thread-backed job execution and bounded result retention."""

import threading
import time
import uuid
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path


def job_id() -> str:
    return "job_" + uuid.uuid4().hex[:12]


class DurableJobStore:
    """SQLite-backed owner-scoped job state and idempotency registry."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as database:
            database.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL, request_fingerprint TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
                created_at REAL NOT NULL, started_at REAL, finished_at REAL,
                result_json TEXT, error_code TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
                estimated_cost REAL NOT NULL DEFAULT 0, actual_cost REAL NOT NULL DEFAULT 0,
                UNIQUE(owner_id, idempotency_key)
            )""")
            columns = {row[1] for row in database.execute("PRAGMA table_info(jobs)")}
            if "request_fingerprint" not in columns:
                database.execute("ALTER TABLE jobs ADD COLUMN request_fingerprint TEXT NOT NULL DEFAULT ''")

    def _connect(self):
        database = sqlite3.connect(self.path, timeout=10)
        database.row_factory = sqlite3.Row
        return database

    @staticmethod
    def _serialize(row) -> dict | None:
        if not row:
            return None
        item = dict(row)
        raw_result = item.pop("result_json")
        item["result"] = json.loads(raw_result) if raw_result else None
        item["cancel_requested"] = bool(item["cancel_requested"])
        return item

    def create_or_get(self, owner_id: str, idempotency_key: str, request_fingerprint: str, estimated_cost: float) -> tuple[dict, bool]:
        if not owner_id or not idempotency_key or len(idempotency_key) > 128:
            raise ValueError("A valid owner and idempotency key are required")
        identifier = job_id()
        created_at = time.time()
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            existing = database.execute(
                "SELECT * FROM jobs WHERE owner_id=? AND idempotency_key=?",
                (owner_id, idempotency_key),
            ).fetchone()
            if existing:
                if existing["request_fingerprint"] and existing["request_fingerprint"] != request_fingerprint:
                    database.rollback()
                    raise ValueError("Idempotency key was already used for a different request")
                database.rollback()
                return self._serialize(existing), False
            database.execute(
                "INSERT INTO jobs (id,owner_id,idempotency_key,request_fingerprint,status,created_at,estimated_cost) VALUES (?,?,?,?,?,?,?)",
                (identifier, owner_id, idempotency_key, request_fingerprint, "queued", created_at, estimated_cost),
            )
            database.commit()
            row = database.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone()
        return self._serialize(row), True

    def get(self, identifier: str, owner_id: str) -> dict | None:
        with self._connect() as database:
            row = database.execute(
                "SELECT * FROM jobs WHERE id=? AND owner_id=?", (identifier, owner_id)
            ).fetchone()
        return self._serialize(row)

    def update(self, identifier: str, *, status: str | None = None, result=None, error_code: str | None = None, actual_cost: float | None = None) -> None:
        fields, values = [], []
        if status is not None:
            fields.extend(["status=?", "started_at=COALESCE(started_at,?)"])
            values.extend([status, time.time()])
            if status in {"completed", "failed", "cancelled"}:
                fields.append("finished_at=?")
                values.append(time.time())
        if result is not None:
            fields.append("result_json=?")
            values.append(json.dumps(result))
        if error_code is not None:
            fields.append("error_code=?")
            values.append(error_code)
        if actual_cost is not None:
            fields.append("actual_cost=?")
            values.append(actual_cost)
        if not fields:
            return
        values.append(identifier)
        with self._connect() as database:
            database.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id=?", values)
            database.commit()

    def request_cancel(self, identifier: str, owner_id: str) -> dict | None:
        with self._connect() as database:
            changed = database.execute(
                "UPDATE jobs SET cancel_requested=1,status=CASE WHEN status='queued' THEN 'cancelled' ELSE 'cancel_requested' END,finished_at=CASE WHEN status='queued' THEN ? ELSE finished_at END WHERE id=? AND owner_id=? AND status IN ('queued','running')",
                (time.time(), identifier, owner_id),
            )
            database.commit()
            row = database.execute("SELECT * FROM jobs WHERE id=? AND owner_id=?", (identifier, owner_id)).fetchone()
        return self._serialize(row) if changed.rowcount or row else None

    def cancellation_requested(self, identifier: str) -> bool:
        with self._connect() as database:
            row = database.execute("SELECT cancel_requested FROM jobs WHERE id=?", (identifier,)).fetchone()
        return bool(row and row[0])

    def recover_interrupted(self) -> int:
        """Never replay unknown provider calls; preserve partial data and mark interruption."""
        with self._connect() as database:
            cursor = database.execute(
                "UPDATE jobs SET status=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'failed' END,error_code=CASE WHEN cancel_requested=1 THEN error_code ELSE 'service_restarted' END,finished_at=? WHERE status IN ('queued','running','cancel_requested')",
                (time.time(),),
            )
            database.commit()
        return cursor.rowcount


def evict_old_jobs(
    jobs: dict[str, dict],
    *,
    max_jobs: int,
    ttl_seconds: int,
    now: float | None = None,
) -> None:
    """Drop expired and oldest completed jobs while retaining active work."""
    current_time = time.time() if now is None else now
    stale = [
        identifier
        for identifier, job in jobs.items()
        if job.get("finished_at")
        and (current_time - job["finished_at"]) > ttl_seconds
    ]
    for identifier in stale:
        jobs.pop(identifier, None)

    if len(jobs) <= max_jobs:
        return
    completed = sorted(
        (
            (identifier, job)
            for identifier, job in jobs.items()
            if job.get("status") in ("done", "error")
        ),
        key=lambda item: item[1].get("finished_at") or item[1].get("started_at") or 0,
    )
    while len(jobs) > max_jobs and completed:
        identifier, _ = completed.pop(0)
        jobs.pop(identifier, None)


def run_job_background(
    identifier: str,
    func: Callable,
    *args,
    jobs: dict[str, dict],
    lock: threading.Lock,
    evict: Callable[[], None],
    **kwargs,
) -> None:
    """Run a callable in a daemon thread and persist its terminal state."""

    def worker():
        try:
            result = func(*args, **kwargs)
            with lock:
                jobs[identifier]["status"] = "done"
                jobs[identifier]["result"] = result
                jobs[identifier]["finished_at"] = time.time()
                evict()
        except Exception as exc:
            with lock:
                jobs[identifier]["status"] = "error"
                jobs[identifier]["error"] = str(exc)
                jobs[identifier]["finished_at"] = time.time()
                evict()

    with lock:
        jobs[identifier] = {
            "status": "running",
            "started_at": time.time(),
            "result": None,
            "error": None,
            "finished_at": None,
        }
        evict()
    threading.Thread(target=worker, daemon=True).start()
