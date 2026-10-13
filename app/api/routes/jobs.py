"""Crawl job endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.mappers import job_to_out
from app.api.schemas import (
    CreateJobRequest,
    JobListOut,
    JobOut,
    PlatformOut,
    WorkerStatusOut,
)
from app.core.config import get_settings
from app.core.enums import JobStatus
from app.crawler import adapter_descriptors
from app.db.repositories import JobRepository
from app.db.session import get_db_session

router = APIRouter(prefix="/api", tags=["jobs"])


@router.get("/platforms", response_model=list[PlatformOut])
async def list_platforms() -> list[dict[str, object]]:
    return adapter_descriptors()


@router.get("/worker", response_model=WorkerStatusOut)
async def worker_status(request: Request) -> dict[str, object]:
    return request.app.state.job_manager.snapshot()


@router.post("/jobs", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def create_job(
    payload: CreateJobRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> JobOut:
    settings = get_settings()
    repository = JobRepository(session)

    job = await repository.create_job(
        keyword=payload.keyword,
        platforms=payload.platforms,
        search_filter=payload.search_filter,
        max_posts=payload.max_posts or settings.default_max_posts,
        max_comments_per_post=(
            payload.max_comments_per_post
            if payload.max_comments_per_post is not None
            else settings.default_max_comments
        ),
        include_comments=payload.include_comments,
    )

    # Commit before queueing: the worker opens its own session and would not
    # see a job that is still sitting in this request's uncommitted transaction.
    await session.commit()

    targets = await repository.list_targets(job.id)
    await request.app.state.job_manager.submit(job.id)
    return job_to_out(job, targets)


@router.get("/jobs", response_model=JobListOut)
async def list_jobs(
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    job_status: JobStatus | None = Query(default=None, alias="status"),
    session: AsyncSession = Depends(get_db_session),
) -> JobListOut:
    repository = JobRepository(session)
    jobs, total = await repository.list_jobs(limit=limit, offset=offset, status=job_status)
    items = [job_to_out(job, await repository.list_targets(job.id)) for job in jobs]
    return JobListOut(items=items, total=total)


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(
    job_id: int, session: AsyncSession = Depends(get_db_session)
) -> JobOut:
    repository = JobRepository(session)
    job = await repository.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return job_to_out(job, await repository.list_targets(job_id))


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
async def cancel_job(
    job_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> JobOut:
    repository = JobRepository(session)
    job = await repository.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if JobStatus(job.status).is_terminal:
        raise HTTPException(status_code=409, detail=f"Job {job_id} has already finished")

    if not request.app.state.job_manager.cancel(job_id):
        # Not held by the worker (for example after a restart) - close it here.
        await repository.cancel_unfinished_targets(job_id)
        await repository.finish_job(job_id, JobStatus.CANCELLED, error="cancelled by user")

    await session.commit()
    job = await repository.get(job_id)
    return job_to_out(job, await repository.list_targets(job_id))
