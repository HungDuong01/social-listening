"""Read path used by the HTTP API and the exporter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.enums import Platform
from app.db.models import Author, Comment, CrawlJob, JobPost, Post  # noqa: F401


@dataclass(slots=True)
class PostFilters:
    job_id: int | None = None
    platform: Platform | None = None
    keyword: str | None = None
    author: str | None = None
    min_likes: int | None = None
    order_by: str = "last_seen_at"
    descending: bool = True


_SORTABLE = {
    "last_seen_at": Post.last_seen_at,
    "posted_at": Post.posted_at,
    "likes": Post.likes,
    "comments_count": Post.comments_count,
    "shares": Post.shares,
    "views": Post.views,
}


class QueryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _apply_filters(self, statement: Select, filters: PostFilters) -> Select:
        if filters.job_id is not None:
            statement = statement.join(JobPost, JobPost.post_id == Post.id).where(
                JobPost.job_id == filters.job_id
            )
        if filters.platform is not None:
            statement = statement.where(Post.platform == filters.platform.value)
        if filters.keyword:
            needle = f"%{filters.keyword.lower()}%"
            statement = statement.where(func.lower(Post.content).like(needle))
        if filters.author:
            statement = statement.join(Author, Author.id == Post.author_id).where(
                func.lower(Author.username) == filters.author.lower()
            )
        if filters.min_likes is not None:
            statement = statement.where(Post.likes >= filters.min_likes)
        return statement

    async def list_posts(
        self, filters: PostFilters, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[Post], int]:
        count_statement = self._apply_filters(select(func.count(Post.id)), filters)
        total = await self._session.scalar(count_statement)

        column = _SORTABLE.get(filters.order_by, Post.last_seen_at)
        ordering = column.desc() if filters.descending else column.asc()

        statement = self._apply_filters(select(Post), filters)
        statement = statement.order_by(ordering, Post.id.desc()).limit(limit).offset(offset)
        rows = await self._session.scalars(statement)
        return list(rows.unique()), int(total or 0)

    async def get_post(self, post_id: int) -> Post | None:
        return await self._session.get(Post, post_id)

    async def list_comments(
        self, post_id: int, *, limit: int = 500, offset: int = 0
    ) -> list[Comment]:
        rows = await self._session.scalars(
            select(Comment)
            .where(Comment.post_id == post_id)
            .order_by(Comment.depth.asc(), Comment.id.asc())
            .limit(limit)
            .offset(offset)
        )
        return list(rows.unique())

    async def comments_for_posts(self, post_ids: Sequence[int]) -> dict[int, list[Comment]]:
        if not post_ids:
            return {}
        rows = await self._session.scalars(
            select(Comment)
            .where(Comment.post_id.in_(post_ids))
            .order_by(Comment.post_id.asc(), Comment.depth.asc(), Comment.id.asc())
        )
        grouped: dict[int, list[Comment]] = {post_id: [] for post_id in post_ids}
        for comment in rows.unique():
            grouped.setdefault(comment.post_id, []).append(comment)
        return grouped

    async def job_post_records(
        self, post_ids: Sequence[int], *, job_id: int | None = None
    ) -> dict[int, JobPost]:
        """Per-run record for each post: rank, notes, how many comments it got.

        With no ``job_id`` a post may belong to several runs, so the most
        recent one wins - that is the crawl whose notes still describe the
        stored data.
        """
        if not post_ids:
            return {}

        statement = select(JobPost).where(JobPost.post_id.in_(post_ids))
        if job_id is not None:
            statement = statement.where(JobPost.job_id == job_id)
        statement = statement.order_by(JobPost.post_id, JobPost.found_at.asc())

        records: dict[int, JobPost] = {}
        for row in await self._session.scalars(statement):
            records[row.post_id] = row  # later found_at overwrites earlier
        return records

    async def posts_for_export(self, filters: PostFilters, *, limit: int = 5_000) -> list[Post]:
        statement = self._apply_filters(select(Post), filters)
        statement = statement.order_by(Post.platform.asc(), Post.id.asc()).limit(limit)
        rows = await self._session.scalars(statement)
        return list(rows.unique())

    async def job_with_targets(self, job_id: int) -> CrawlJob | None:
        statement = (
            select(CrawlJob).where(CrawlJob.id == job_id).options(selectinload(CrawlJob.targets))
        )
        return await self._session.scalar(statement)

    async def stats(self) -> dict[str, Any]:
        totals = {
            "posts": int(await self._session.scalar(select(func.count(Post.id))) or 0),
            "comments": int(await self._session.scalar(select(func.count(Comment.id))) or 0),
            "authors": int(await self._session.scalar(select(func.count(Author.id))) or 0),
            "jobs": int(await self._session.scalar(select(func.count(CrawlJob.id))) or 0),
        }

        rows = await self._session.execute(
            select(
                Post.platform,
                func.count(Post.id),
                func.coalesce(func.sum(Post.likes), 0),
                func.coalesce(func.sum(Post.comments_collected), 0),
            ).group_by(Post.platform)
        )
        totals["by_platform"] = [
            {
                "platform": platform,
                "posts": int(count),
                "likes": int(likes),
                "comments": int(comments),
            }
            for platform, count, likes, comments in rows.all()
        ]
        return totals
