"""Queues crawl jobs and runs them one at a time.

A single worker is intentional: every job drives a real browser, and running
several concurrently multiplies memory use while making each one look more
like automated traffic. Concurrency lives *inside* a job (scroll + intercept),
not across jobs.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from app.core.config import Settings, get_settings
from app.core.enums import JobStatus
from app.core.logging import get_logger
from app.core.utils import utcnow
from app.crawler import BrowserManager
from app.db.repositories import JobRepository
from app.db.session import session_scope
from app.services.crawl_service import CrawlService

logger = get_logger(__name__)


class JobManager:
    def __init__(self, browser: BrowserManager, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._service = CrawlService(browser, self._settings)
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._cancel_events: dict[int, asyncio.Event] = {}
        self._worker: asyncio.Task[None] | None = None
        self._current_job: int | None = None
        self._started_at: datetime | None = None

    # -------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self._worker is not None:
            return
        await self._reconcile_interrupted_jobs()
        self._worker = asyncio.create_task(self._run_worker(), name="crawl-worker")
        logger.info("Job worker started")

    async def stop(self) -> None:
        for event in self._cancel_events.values():
            event.set()

        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None

        if self._current_job is not None:
            await self._mark_interrupted(self._current_job, "server shut down mid-crawl")
            self._current_job = None
        logger.info("Job worker stopped")

    async def _reconcile_interrupted_jobs(self) -> None:
        """A job left RUNNING belongs to a process that no longer exists."""
        stale_ids: list[int] = []
        async with session_scope() as session:
            repository = JobRepository(session)
            stale_ids = await repository.pending_job_ids()
            for job_id in stale_ids:
                await repository.cancel_unfinished_targets(job_id)
                await repository.finish_job(
                    job_id, JobStatus.CANCELLED, error="interrupted by a server restart"
                )
        if stale_ids:
            logger.warning("Reset %s interrupted job(s): %s", len(stale_ids), stale_ids)

    # ----------------------------------------------------------------- public

    async def submit(self, job_id: int) -> None:
        self._cancel_events.setdefault(job_id, asyncio.Event())
        await self._queue.put(job_id)
        logger.info("Job %s queued (queue depth=%s)", job_id, self._queue.qsize())

    def cancel(self, job_id: int) -> bool:
        event = self._cancel_events.get(job_id)
        if event is None:
            return False
        event.set()
        logger.info("Job %s cancellation requested", job_id)
        return True

    @property
    def current_job_id(self) -> int | None:
        return self._current_job

    def snapshot(self) -> dict[str, object]:
        return {
            "running": self._current_job,
            "queued": self._queue.qsize(),
            "worker_alive": self._worker is not None and not self._worker.done(),
            "started_at": self._started_at.isoformat() if self._started_at else None,
        }

    # ----------------------------------------------------------------- worker

    async def _run_worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                await self._execute(job_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the worker must outlive a bad job
                logger.exception("Unhandled failure while running job %s", job_id)
            finally:
                self._queue.task_done()

    async def _execute(self, job_id: int) -> None:
        cancel_event = self._cancel_events.setdefault(job_id, asyncio.Event())

        if cancel_event.is_set():
            await self._mark_interrupted(job_id, "cancelled before it started")
            self._cancel_events.pop(job_id, None)
            return

        self._current_job = job_id
        self._started_at = utcnow()
        try:
            await asyncio.wait_for(
                self._service.run_job(job_id, cancel_event),
                timeout=self._settings.job_timeout_s,
            )
        except asyncio.TimeoutError:
            cancel_event.set()
            logger.error("Job %s exceeded %ss and was aborted", job_id, self._settings.job_timeout_s)
            await self._mark_failed(job_id, f"timed out after {self._settings.job_timeout_s}s")
        except asyncio.CancelledError:
            await self._mark_interrupted(job_id, "cancelled while running")
            raise
        except Exception as error:  # noqa: BLE001
            logger.exception("Job %s failed", job_id)
            await self._mark_failed(job_id, f"{type(error).__name__}: {error}")
        finally:
            self._current_job = None
            self._started_at = None
            self._cancel_events.pop(job_id, None)

    # ---------------------------------------------------------------- helpers

    async def _mark_failed(self, job_id: int, reason: str) -> None:
        async with session_scope() as session:
            repository = JobRepository(session)
            await repository.cancel_unfinished_targets(job_id)
            await repository.finish_job(job_id, JobStatus.FAILED, error=reason)

    async def _mark_interrupted(self, job_id: int, reason: str) -> None:
        async with session_scope() as session:
            repository = JobRepository(session)
            await repository.cancel_unfinished_targets(job_id)
            await repository.finish_job(job_id, JobStatus.CANCELLED, error=reason)
