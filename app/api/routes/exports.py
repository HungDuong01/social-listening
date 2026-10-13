"""Export endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import ExportRequest
from app.db.repositories import JobRepository, PostFilters
from app.db.session import get_db_session

router = APIRouter(prefix="/api/exports", tags=["exports"])

_MEDIA_TYPES = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv",
    "json": "application/json",
}


@router.post("")
async def create_export(
    payload: ExportRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> FileResponse:
    label = payload.keyword or "export"
    if payload.job_id is not None:
        job = await JobRepository(session).get(payload.job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {payload.job_id} not found")
        label = job.keyword

    filters = PostFilters(
        job_id=payload.job_id,
        platform=payload.platform,
        keyword=payload.keyword,
    )

    try:
        path = await request.app.state.export_service.export(
            session, filters=filters, fmt=payload.format, label=label, limit=payload.limit
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    return FileResponse(
        path=path,
        filename=path.name,
        media_type=_MEDIA_TYPES.get(payload.format, "application/octet-stream"),
    )
