from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4


TERMINAL_STATUSES = {"succeeded", "failed"}


class AnalysisJobNotFoundError(KeyError):
    pass


@dataclass
class AnalysisJob:
    id: str
    status: str
    params: dict[str, Any]
    message: str
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    traceback: str | None = None
    thread: threading.Thread | None = field(default=None, repr=False)

    def as_dict(self, *, include_traceback: bool = False) -> dict[str, Any]:
        payload = {
            "id": self.id,
            "status": self.status,
            "params": self.params,
            "message": self.message,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "error": self.error,
        }
        if include_traceback:
            payload["traceback"] = self.traceback
        return payload


_jobs: dict[str, AnalysisJob] = {}
_jobs_lock = threading.Lock()


def start_analysis_job(
    *,
    params: dict[str, Any],
    message: str,
    target: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    with _jobs_lock:
        job = AnalysisJob(
            id=uuid4().hex,
            status="queued",
            params=params,
            message=message,
            created_at=_now(),
        )
        thread = threading.Thread(target=_run_job, args=(job.id, target), daemon=True)
        job.thread = thread
        _jobs[job.id] = job
        _prune_finished_jobs()
        thread.start()
        return job.as_dict()


def get_analysis_job(job_id: str) -> dict[str, Any]:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            raise AnalysisJobNotFoundError(job_id)
        return job.as_dict()


def _run_job(job_id: str, target: Callable[[], dict[str, Any]]) -> None:
    with _jobs_lock:
        job = _jobs[job_id]
        job.status = "running"
        job.started_at = _now()
        job.message = "Analyse läuft."

    try:
        result = target()
    except Exception as exc:  # pragma: no cover - defensive for background jobs.
        with _jobs_lock:
            job = _jobs[job_id]
            job.status = "failed"
            job.finished_at = _now()
            job.error = str(exc)
            job.message = f"Analyse fehlgeschlagen: {exc}"
            job.traceback = traceback.format_exc()
    else:
        with _jobs_lock:
            job = _jobs[job_id]
            job.status = "succeeded"
            job.finished_at = _now()
            job.result = result
            job.message = "Analyse abgeschlossen."


def _prune_finished_jobs(limit: int = 50) -> None:
    if len(_jobs) <= limit:
        return
    finished = [
        (job.finished_at or job.created_at, job_id)
        for job_id, job in _jobs.items()
        if job.status in TERMINAL_STATUSES
    ]
    for _timestamp, job_id in sorted(finished)[: max(0, len(_jobs) - limit)]:
        _jobs.pop(job_id, None)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
