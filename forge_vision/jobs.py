"""Long-running job interface (FR-API-003).

Migration over a large site, a wide band survey, or narration by a local
language model all take long enough that holding an HTTP request open is the
wrong shape. The spec asks for jobs that can be submitted, monitored,
cancelled, retried, and inspected, and for §12's requirement that heavy
processing "run asynchronously and report progress".

Cancellation is cooperative: a job function is handed a `JobContext` and is
expected to call `ctx.check()` between units of work. That is deliberate —
killing a thread mid-write could leave a half-finished experiment package on
disk, and FR-DAT-003 requires the opposite. A job that ignores its context
simply runs to completion and reports as cancelled afterwards.
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"

TERMINAL = (SUCCEEDED, FAILED, CANCELLED)


class JobCancelled(Exception):
    """Raised inside a job when the operator has asked it to stop."""


class JobContext:
    """Handed to the job function: progress reporting and cancellation."""

    def __init__(self, job: "Job"):
        self._job = job

    def progress(self, fraction: float, message: str = "") -> None:
        self._job.progress = max(0.0, min(1.0, float(fraction)))
        if message:
            self._job.message = message

    def check(self) -> None:
        """Raise JobCancelled if cancellation has been requested."""
        if self._job.cancel_requested:
            raise JobCancelled()

    @property
    def cancelled(self) -> bool:
        return self._job.cancel_requested


class Job:
    def __init__(self, kind: str, description: str, params: dict | None = None):
        self.job_id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.description = description
        self.params = params or {}
        self.state = QUEUED
        self.progress = 0.0
        self.message = "queued"
        self.result = None
        self.error = ""
        self.traceback = ""
        self.cancel_requested = False
        self.created_at = time.time()
        self.started_at = None
        self.ended_at = None
        self.attempts = 0

    def to_dict(self, include_result: bool = False) -> dict:
        d = {
            "job_id": self.job_id, "kind": self.kind,
            "description": self.description, "params": self.params,
            "state": self.state, "progress": round(self.progress, 3),
            "message": self.message, "error": self.error,
            "created_at": self.created_at, "started_at": self.started_at,
            "ended_at": self.ended_at, "attempts": self.attempts,
            "cancel_requested": self.cancel_requested,
            "duration_s": (round((self.ended_at or time.time())
                                 - self.started_at, 2)
                           if self.started_at else None),
        }
        if include_result:
            d["result"] = self.result
            d["traceback"] = self.traceback
        return d


class JobManager:
    """A small bounded queue of cancellable background jobs."""

    def __init__(self, max_workers: int = 2, history: int = 200):
        self._pool = ThreadPoolExecutor(max_workers=max_workers,
                                        thread_name_prefix="fv-job")
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._fns: dict[str, tuple] = {}
        self._lock = threading.RLock()
        self._history = history

    def submit(self, kind: str, description: str, fn, params: dict | None = None) -> Job:
        """`fn(ctx)` runs on a worker thread; ctx reports progress and
        cancellation."""
        job = Job(kind, description, params)
        with self._lock:
            self._jobs[job.job_id] = job
            self._fns[job.job_id] = (fn,)
            self._trim()
        self._pool.submit(self._run, job)
        return job

    def _run(self, job: Job) -> None:
        if job.cancel_requested:          # cancelled before a worker picked it up
            job.state = CANCELLED
            job.message = "cancelled before starting"
            job.ended_at = time.time()
            return
        job.state = RUNNING
        job.started_at = time.time()
        job.attempts += 1
        job.message = "running"
        fn = self._fns[job.job_id][0]
        # Details are always assigned *before* the state transition: a caller
        # polling for a terminal state must never observe one without the
        # result, error, or traceback that explains it.
        try:
            result = fn(JobContext(job))
            job.result = result
            if job.cancel_requested:
                # the function returned without honouring the request; report
                # honestly rather than pretending the work was stopped
                job.message = "cancelled (work had already completed)"
                job.ended_at = time.time()
                job.state = CANCELLED
            else:
                job.progress = 1.0
                job.message = "complete"
                job.ended_at = time.time()
                job.state = SUCCEEDED
        except JobCancelled:
            job.message = "cancelled"
            job.ended_at = time.time()
            job.state = CANCELLED
        except Exception as exc:  # noqa: BLE001 - a failed job must not kill the pool
            job.error = str(exc)
            job.traceback = traceback.format_exc()
            job.message = "failed"
            job.ended_at = time.time()
            job.state = FAILED

    def get(self, job_id: str) -> Job:
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(f"unknown job: {job_id}")
            return self._jobs[job_id]

    def list(self, kind: str = "", active_only: bool = False) -> list[dict]:
        with self._lock:
            jobs = list(self._jobs.values())
        out = [j for j in jobs
               if (not kind or j.kind == kind)
               and (not active_only or j.state not in TERMINAL)]
        return [j.to_dict() for j in reversed(out)]

    def cancel(self, job_id: str) -> dict:
        job = self.get(job_id)
        if job.state in TERMINAL:
            return job.to_dict()
        job.cancel_requested = True
        job.message = "cancellation requested"
        return job.to_dict()

    def retry(self, job_id: str) -> Job:
        """Resubmit a finished job with the same work function."""
        old = self.get(job_id)
        if old.state not in TERMINAL:
            raise ValueError(f"job {job_id} is still {old.state}")
        with self._lock:
            fn = self._fns[job_id][0]
        return self.submit(old.kind, old.description, fn, old.params)

    def summary(self) -> dict:
        with self._lock:
            jobs = list(self._jobs.values())
        counts: dict[str, int] = {}
        for j in jobs:
            counts[j.state] = counts.get(j.state, 0) + 1
        return {"counts": counts,
                "active": [j.to_dict() for j in jobs if j.state not in TERMINAL]}

    def _trim(self) -> None:
        while len(self._jobs) > self._history:
            for jid, job in list(self._jobs.items()):
                if job.state in TERMINAL:
                    self._jobs.pop(jid, None)
                    self._fns.pop(jid, None)
                    break
            else:
                return

    def shutdown(self, wait: bool = False) -> None:
        self._pool.shutdown(wait=wait, cancel_futures=True)
