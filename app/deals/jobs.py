"""Background job store for wide searches.

A wide search takes minutes, which is far too long for an agent to hold an
HTTP request open. Callers POST a search, get a job id, and poll — seeing
live progress and partial results rather than a spinner.

In-memory and single-process: fine for a personal tool, and the seam to
swap in Redis later is `JobStore`.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from app.deals.engine import run_deal_search
from app.deals.planner import PlanError, plan_search
from app.deals.schemas import DealSearchRequest, DealSearchResult

logger = logging.getLogger(__name__)

JobStatus = Literal["queued", "running", "complete", "failed"]

MAX_JOBS = 200


class JobProgress(BaseModel):
    """How far along a running search is."""

    stage: str = "queued"
    done: int = 0
    total: int = 0

    @property
    def percent(self) -> int:
        """Completion percentage, or 0 when the total is unknown."""
        return int(100 * self.done / self.total) if self.total else 0


class Job(BaseModel):
    """One wide search, from submission to result."""

    id: str
    status: JobStatus = "queued"
    created_at: datetime
    progress: JobProgress = Field(default_factory=JobProgress)
    estimated_requests: int | None = None
    result: DealSearchResult | None = None
    error: str | None = None


class JobStore:
    """Thread-safe job registry with a bounded executor."""

    def __init__(self, max_workers: int = 2):
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._lock = threading.Lock()
        # Deliberately small: concurrent searches multiply the request rate
        # against the upstream provider.
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="deals")

    def submit(self, request: DealSearchRequest, provider) -> Job:
        """Validate the plan, register a job, and start it in the background."""
        # Planning is pure and fast — failing here gives the caller a real
        # error instead of a job that dies a second later.
        plan = plan_search(request)

        job = Job(
            id=uuid.uuid4().hex[:12],
            created_at=datetime.now(timezone.utc),
            estimated_requests=plan.estimated_requests,
            progress=JobProgress(stage="queued", done=0, total=plan.estimated_requests),
        )
        with self._lock:
            self._jobs[job.id] = job
            while len(self._jobs) > MAX_JOBS:
                self._jobs.popitem(last=False)

        self._executor.submit(self._run, job.id, request, provider)
        return job

    def _run(self, job_id: str, request: DealSearchRequest, provider) -> None:
        """Execute one search, recording progress and the outcome."""
        def progress(stage: str, done: int, total: int) -> None:
            with self._lock:
                job = self._jobs.get(job_id)
                if job:
                    job.progress = JobProgress(stage=stage, done=done, total=total)

        with self._lock:
            self._jobs[job_id].status = "running"
        try:
            result = run_deal_search(request, provider, progress)
            with self._lock:
                job = self._jobs[job_id]
                job.result = result
                job.status = "complete"
                job.progress = JobProgress(stage="complete", done=1, total=1)
        except (PlanError, Exception) as exc:  # noqa: BLE001 - surfaced to the caller
            logger.exception("deal search job %s failed", job_id)
            with self._lock:
                job = self._jobs[job_id]
                job.status = "failed"
                job.error = str(exc)

    def get(self, job_id: str) -> Job | None:
        """Look up a job by id."""
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, limit: int = 20) -> list[Job]:
        """Most recent jobs first."""
        with self._lock:
            return list(reversed(list(self._jobs.values())))[:limit]


store = JobStore()
