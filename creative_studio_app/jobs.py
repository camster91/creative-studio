"""Thread-backed job execution and bounded result retention."""

import threading
import time
import uuid
from collections.abc import Callable


def job_id() -> str:
    return "job_" + uuid.uuid4().hex[:12]


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
