"""Read endpoints over the crawled dataset."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.mappers import comment_to_out, post_to_out
from app.api.schemas import PostDetailOut, PostListOut, StatsOut
from app.core.enums import Platform
from app.db.repositories import PostFilters, QueryRepository
from app.db.session import get_db_session

router = APIRouter(prefix="/api", tags=["data"])


@router.get("/posts", response_model=PostListOut)
async def list_posts(
    job_id: int | None = Query(default=None),
    platform: Platform | None = Query(default=None),
    keyword: str | None = Query(default=None, max_length=200),
    author: str | None = Query(default=None, max_length=200),
    min_likes: int | None = Query(default=None, ge=0),
    order_by: str = Query(default="last_seen_at"),
    descending: bool = Query(default=True),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db_session),
) -> PostListOut:
    filters = PostFilters(
        job_id=job_id,
        platform=platform,
        keyword=keyword,
        author=author,
        min_likes=min_likes,
        order_by=order_by,
        descending=descending,
    )
    repository = QueryRepository(session)
    posts, total = await repository.list_posts(filters, limit=limit, offset=offset)
    return PostListOut(
        items=[post_to_out(post) for post in posts],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/posts/{post_id}", response_model=PostDetailOut)
async def get_post(
    post_id: int,
    comment_limit: int = Query(default=500, ge=1, le=5_000),
    session: AsyncSession = Depends(get_db_session),
) -> PostDetailOut:
    repository = QueryRepository(session)
    post = await repository.get_post(post_id)
    if post is None:
        raise HTTPException(status_code=404, detail=f"Post {post_id} not found")

    comments = await repository.list_comments(post_id, limit=comment_limit)
    return PostDetailOut(
        post=post_to_out(post),
        comments=[comment_to_out(comment) for comment in comments],
    )


@router.get("/stats", response_model=StatsOut)
async def get_stats(session: AsyncSession = Depends(get_db_session)) -> StatsOut:
    repository = QueryRepository(session)
    return StatsOut(**await repository.stats())
