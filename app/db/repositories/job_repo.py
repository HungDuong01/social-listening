"""Job + per-platform target lifecycle."""

from __future__ import annotations

from typing import Any, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import JobStatus, Platform, SearchFilter, TargetStatus
from app.core.utils import utcnow
from app.db.dialects import upsert
from app.db.models import CrawlJob, JobTarget


class JobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --------------------------------------------------------------- creation

    async def create_job(
        self,
        *,
        keyword: str,
        platforms: Sequence[Platform],
        search_filter: SearchFilter,
        max_posts: int,
        max_comments_per_post: int,
        include_comments: bool,
    ) -> CrawlJob:
        job = CrawlJob(
            keyword=keyword,
            platforms=[platform.value for platform in platforms],
            search_filter=search_filter.value,
            max_posts=max_posts,
            max_comments_per_post=max_comments_per_post,
            include_comments=include_comments,
            status=JobStatus.PENDING.value,
        )
        self._session.add(job)
        await self._session.flush()

        for platform in platforms:
            self._session.add(
                JobTarget(
                    job_id=job.id,
                    platform=platform.value,
                    status=TargetStatus.PENDING.value,
                )
            )
        await self._session.flush()
        return job

    # ---------------------------------------------------------------- reading

    async def get(self, job_id: int) -> CrawlJob | None:
        return await self._session.get(CrawlJob, job_id)

    async def list_jobs(
        self, *, limit: int = 25, offset: int = 0, status: JobStatus | None = None
    ) -> tuple[list[CrawlJob], int]:
        conditions = []
        if status is not None:
            conditions.append(CrawlJob.status == status.value)

        total = await self._session.scalar(
            select(func.count()).select_from(CrawlJob).where(*conditions)
        )
        rows = await self._session.scalars(
            select(CrawlJob)
            .where(*conditions)
            .order_by(CrawlJob.created_at.desc(), CrawlJob.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(rows), int(total or 0)

    async def list_targets(self, job_id: int) -> list[JobTarget]:
        rows = await self._session.scalars(
            select(JobTarget).where(JobTarget.job_id == job_id).order_by(JobTarget.id)
        )
        return list(rows)

    async def pending_job_ids(self) -> list[int]:
        rows = await self._session.scalars(
            select(CrawlJob.id)
            .where(CrawlJob.status.in_([JobStatus.PENDING.value, JobStatus.RUNNING.value]))
            .order_by(CrawlJob.id)
        )
        return list(rows)

    # ---------------------------------------------------------------- updates

    async def mark_running(self, job_id: int) -> None:
        await self._session.execute(
            update(CrawlJob)
            .where(CrawlJob.id == job_id)
            .values(status=JobStatus.RUNNING.value, started_at=utcnow(), error=None)
        )

    async def finish_job(self, job_id: int, status: JobStatus, error: str | None = None) -> None:
        await self._session.execute(
            update(CrawlJob)
            .where(CrawlJob.id == job_id)
            .values(status=status.value, finished_at=utcnow(), error=error)
        )

    async def update_target(
        self, job_id: int, platform: Platform, /, **values: Any
    ) -> None:
        if not values:
            return
        await self._session.execute(
            update(JobTarget)
            .where(JobTarget.job_id == job_id, JobTarget.platform == platform.value)
            .values(**values)
        )

    async def start_target(self, job_id: int, platform: Platform, search_url: str) -> None:
        await self.update_target(
            job_id,
            platform,
            status=TargetStatus.RUNNING.value,
            search_url=search_url,
            started_at=utcnow(),
            error=None,
        )

    async def finish_target(
        self,
        job_id: int,
        platform: Platform,
        status: TargetStatus,
        *,
        error: str | None = None,
        message: str | None = None,
    ) -> None:
        await self.update_target(
            job_id,
            platform,
            status=status.value,
            error=error,
            message=message,
            finished_at=utcnow(),
        )

    async def bump_target(
        self,
        job_id: int,
        platform: Platform,
        *,
        posts_found: int = 0,
        posts_crawled: int = 0,
        posts_failed: int = 0,
        comments_crawled: int = 0,
        message: str | None = None,
    ) -> None:
        values: dict[str, Any] = {}
        if posts_found:
            values["posts_found"] = JobTarget.posts_found + posts_found
        if posts_crawled:
            values["posts_crawled"] = JobTarget.posts_crawled + posts_crawled
        if posts_failed:
            values["posts_failed"] = JobTarget.posts_failed + posts_failed
        if comments_crawled:
            values["comments_crawled"] = JobTarget.comments_crawled + comments_crawled
        if message is not None:
            values["message"] = message
        if values:
            await self.update_target(job_id, platform, **values)

    async def bump_job(
        self,
        job_id: int,
        *,
        posts_found: int = 0,
        posts_crawled: int = 0,
        comments_crawled: int = 0,
    ) -> None:
        values: dict[str, Any] = {}
        if posts_found:
            values["posts_found"] = CrawlJob.posts_found + posts_found
        if posts_crawled:
            values["posts_crawled"] = CrawlJob.posts_crawled + posts_crawled
        if comments_crawled:
            values["comments_crawled"] = CrawlJob.comments_crawled + comments_crawled
        if values:
            await self._session.execute(
                update(CrawlJob).where(CrawlJob.id == job_id).values(**values)
            )

    async def ensure_target(self, job_id: int, platform: Platform) -> None:
        statement = upsert(self._session, JobTarget).values(
            job_id=job_id, platform=platform.value, status=TargetStatus.PENDING.value
        )
        await self._session.execute(
            statement.on_conflict_do_nothing(index_elements=["job_id", "platform"])
        )

    async def cancel_unfinished_targets(self, job_id: int) -> None:
        await self._session.execute(
            update(JobTarget)
            .where(
                JobTarget.job_id == job_id,
                JobTarget.status.in_([TargetStatus.PENDING.value, TargetStatus.RUNNING.value]),
            )
            .values(status=TargetStatus.CANCELLED.value, finished_at=utcnow())
        )

    # ------------------------------------------------------------- derivation

    async def derive_job_status(self, job_id: int) -> JobStatus:
        """Roll per-platform outcomes up into a single job status."""
        targets = await self.list_targets(job_id)
        if not targets:
            return JobStatus.FAILED

        statuses = {target.status for target in targets}
        if TargetStatus.CANCELLED.value in statuses:
            return JobStatus.CANCELLED
        if statuses == {TargetStatus.COMPLETED.value}:
            return JobStatus.COMPLETED
        if TargetStatus.COMPLETED.value in statuses:
            return JobStatus.PARTIAL
        return JobStatus.FAILED
