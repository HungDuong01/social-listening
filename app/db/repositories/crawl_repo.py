"""Write path: idempotent persistence of crawled entities.

Every method is safe to re-run. Re-crawling the same keyword updates rows in
place instead of duplicating them, and a later crawl that happens to learn
*less* than an earlier one never erases what is already stored - that is what
the ``COALESCE`` guards below are for.
"""

from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.dialects import upsert
from app.core.utils import utcnow
from app.db.models import Author, Comment, JobPost, Post, PostMetricSnapshot
from app.domain.entities import AuthorRef, CrawlResult, Metrics, RawComment, RawPost

logger = get_logger(__name__)


class CrawlRepository:
    """Persists what the adapters produced. One instance per crawl session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._author_ids: dict[tuple[str, str], int] = {}

    # ------------------------------------------------------------------ authors

    async def upsert_author(self, author: AuthorRef) -> int:
        cache_key = (author.platform.value, author.key)
        cached = self._author_ids.get(cache_key)
        if cached is not None:
            return cached

        now = utcnow()
        insert_stmt = upsert(self._session, Author).values(
            platform=author.platform.value,
            identity_key=author.key,
            external_id=author.external_id,
            username=author.username,
            display_name=author.display_name,
            avatar_url=author.avatar_url,
            is_verified=author.is_verified,
            follower_count=author.follower_count,
            first_seen_at=now,
            last_seen_at=now,
        )
        excluded = insert_stmt.excluded
        statement = insert_stmt.on_conflict_do_update(
            index_elements=["platform", "identity_key"],
            set_={
                "username": excluded.username,
                "external_id": func.coalesce(excluded.external_id, Author.external_id),
                "display_name": func.coalesce(excluded.display_name, Author.display_name),
                "avatar_url": func.coalesce(excluded.avatar_url, Author.avatar_url),
                "is_verified": func.coalesce(excluded.is_verified, Author.is_verified),
                "follower_count": func.coalesce(excluded.follower_count, Author.follower_count),
                "last_seen_at": now,
            },
        )
        await self._session.execute(statement)

        author_id = await self._session.scalar(
            select(Author.id).where(
                Author.platform == author.platform.value,
                Author.identity_key == author.key,
            )
        )
        if author_id is None:  # pragma: no cover - only if the row vanished mid-transaction
            raise RuntimeError(f"Author upsert did not yield an id: {author.key}")

        self._author_ids[cache_key] = author_id
        return author_id

    # -------------------------------------------------------------------- posts

    async def upsert_post(self, post: RawPost) -> int:
        author_id = await self.upsert_author(post.author)
        now = utcnow()
        metrics = post.metrics

        insert_stmt = upsert(self._session, Post).values(
            platform=post.platform.value,
            external_id=post.external_id,
            shortcode=post.shortcode,
            url=post.url,
            author_id=author_id,
            content=post.content or "",
            language=post.language,
            posted_at=post.posted_at,
            time_label=post.time_label,
            media=[_media_to_dict(item) for item in post.media],
            hashtags=post.hashtags,
            mentions=post.mentions,
            raw=post.raw,
            likes=metrics.likes,
            comments_count=metrics.comments,
            shares=metrics.shares,
            reposts=metrics.reposts,
            views=metrics.views,
            saves=metrics.saves,
            metrics_source=metrics.source.value,
            metrics_approximate=metrics.approximate,
            first_seen_at=now,
            last_seen_at=now,
        )
        excluded = insert_stmt.excluded

        # Only overwrite a column when this pass actually carries a value for it.
        update_values: dict[str, Any] = {
            "url": excluded.url,
            "author_id": func.coalesce(excluded.author_id, Post.author_id),
            "likes": func.coalesce(excluded.likes, Post.likes),
            "comments_count": func.coalesce(excluded.comments_count, Post.comments_count),
            "shares": func.coalesce(excluded.shares, Post.shares),
            "reposts": func.coalesce(excluded.reposts, Post.reposts),
            "views": func.coalesce(excluded.views, Post.views),
            "saves": func.coalesce(excluded.saves, Post.saves),
            "last_seen_at": now,
        }
        if post.content:
            update_values["content"] = excluded.content
        if post.shortcode:
            update_values["shortcode"] = excluded.shortcode
        if post.posted_at is not None:
            update_values["posted_at"] = excluded.posted_at
        if post.time_label:
            update_values["time_label"] = excluded.time_label
        if post.language:
            update_values["language"] = excluded.language
        if post.media:
            update_values["media"] = excluded.media
        if post.hashtags:
            update_values["hashtags"] = excluded.hashtags
        if post.mentions:
            update_values["mentions"] = excluded.mentions
        if post.raw is not None:
            update_values["raw"] = excluded.raw
        if not metrics.is_empty:
            update_values["metrics_source"] = excluded.metrics_source
            update_values["metrics_approximate"] = excluded.metrics_approximate

        await self._session.execute(
            insert_stmt.on_conflict_do_update(
                index_elements=["platform", "external_id"], set_=update_values
            )
        )

        post_id = await self._session.scalar(
            select(Post.id).where(
                Post.platform == post.platform.value,
                Post.external_id == post.external_id,
            )
        )
        if post_id is None:  # pragma: no cover
            raise RuntimeError(f"Post upsert did not yield an id: {post.external_id}")
        return post_id

    async def record_metric_snapshot(
        self, post_id: int, job_id: int | None, metrics: Metrics
    ) -> None:
        if metrics.is_empty:
            return
        now = utcnow()
        insert_stmt = upsert(self._session, PostMetricSnapshot).values(
            post_id=post_id,
            job_id=job_id,
            likes=metrics.likes,
            comments_count=metrics.comments,
            shares=metrics.shares,
            reposts=metrics.reposts,
            views=metrics.views,
            saves=metrics.saves,
            source=metrics.source.value,
            approximate=metrics.approximate,
            captured_at=now,
        )
        excluded = insert_stmt.excluded
        await self._session.execute(
            insert_stmt.on_conflict_do_update(
                index_elements=["post_id", "job_id"],
                set_={
                    "likes": func.coalesce(excluded.likes, PostMetricSnapshot.likes),
                    "comments_count": func.coalesce(
                        excluded.comments_count, PostMetricSnapshot.comments_count
                    ),
                    "shares": func.coalesce(excluded.shares, PostMetricSnapshot.shares),
                    "reposts": func.coalesce(excluded.reposts, PostMetricSnapshot.reposts),
                    "views": func.coalesce(excluded.views, PostMetricSnapshot.views),
                    "saves": func.coalesce(excluded.saves, PostMetricSnapshot.saves),
                    "source": excluded.source,
                    "approximate": excluded.approximate,
                    "captured_at": now,
                },
            )
        )

    async def link_job_post(
        self,
        job_id: int,
        post_id: int,
        rank: int | None,
        *,
        notes: list[str] | None = None,
        comments_collected: int = 0,
        comments_truncated: bool = False,
    ) -> None:
        insert_stmt = upsert(self._session, JobPost).values(
            job_id=job_id,
            post_id=post_id,
            rank=rank,
            notes=list(notes or []),
            comments_collected=comments_collected,
            comments_truncated=comments_truncated,
            found_at=utcnow(),
        )
        excluded = insert_stmt.excluded
        await self._session.execute(
            insert_stmt.on_conflict_do_update(
                index_elements=["job_id", "post_id"],
                set_={
                    "rank": func.coalesce(excluded.rank, JobPost.rank),
                    "notes": excluded.notes,
                    "comments_collected": excluded.comments_collected,
                    "comments_truncated": excluded.comments_truncated,
                },
            )
        )

    # ----------------------------------------------------------------- comments

    async def upsert_comments(self, post_id: int, comments: Iterable[RawComment]) -> int:
        comment_list = list(comments)
        if not comment_list:
            return 0

        now = utcnow()
        platform_value = comment_list[0].platform.value
        written = 0

        for comment in comment_list:
            author_id = await self.upsert_author(comment.author)
            insert_stmt = upsert(self._session, Comment).values(
                platform=comment.platform.value,
                external_id=comment.external_id,
                post_id=post_id,
                parent_external_id=comment.parent_external_id,
                author_id=author_id,
                content=comment.content or "",
                posted_at=comment.posted_at,
                time_label=comment.time_label,
                like_count=comment.like_count,
                reply_count=comment.reply_count,
                depth=comment.depth,
                is_author_reply=comment.is_author_reply,
                raw=comment.raw,
                first_seen_at=now,
                last_seen_at=now,
            )
            excluded = insert_stmt.excluded
            update_values: dict[str, Any] = {
                "post_id": excluded.post_id,
                "author_id": func.coalesce(excluded.author_id, Comment.author_id),
                "like_count": func.coalesce(excluded.like_count, Comment.like_count),
                "reply_count": func.coalesce(excluded.reply_count, Comment.reply_count),
                "depth": excluded.depth,
                "is_author_reply": excluded.is_author_reply,
                "parent_external_id": func.coalesce(
                    excluded.parent_external_id, Comment.parent_external_id
                ),
                "last_seen_at": now,
            }
            if comment.content:
                update_values["content"] = excluded.content
            if comment.posted_at is not None:
                update_values["posted_at"] = excluded.posted_at
            if comment.time_label:
                update_values["time_label"] = excluded.time_label
            if comment.raw is not None:
                update_values["raw"] = excluded.raw

            await self._session.execute(
                insert_stmt.on_conflict_do_update(
                    index_elements=["platform", "external_id"], set_=update_values
                )
            )
            written += 1

        await self._resolve_comment_parents(post_id, platform_value)
        await self._refresh_comment_count(post_id)
        return written

    async def _resolve_comment_parents(self, post_id: int, platform: str) -> None:
        """Link replies to their parent once the whole batch exists.

        Replies are frequently persisted before the comment they answer, so the
        FK cannot be resolved inline without ordering assumptions that the
        platforms do not guarantee.
        """
        await self._session.execute(
            text(
                """
                UPDATE comments
                   SET parent_id = (
                       SELECT parent.id
                         FROM comments AS parent
                        WHERE parent.platform = :platform
                          AND parent.external_id = comments.parent_external_id
                        LIMIT 1
                   )
                 WHERE comments.post_id = :post_id
                   AND comments.parent_external_id IS NOT NULL
                   AND comments.parent_id IS NULL
                """
            ),
            {"post_id": post_id, "platform": platform},
        )

    async def _refresh_comment_count(self, post_id: int) -> None:
        total = await self._session.scalar(
            select(func.count()).select_from(Comment).where(Comment.post_id == post_id)
        )
        await self._session.execute(
            update(Post).where(Post.id == post_id).values(comments_collected=int(total or 0))
        )

    # ------------------------------------------------------------------ helpers

    async def persist_result(
        self, *, job_id: int, result: CrawlResult, rank: int | None
    ) -> tuple[int, int]:
        """Store one crawled post whole. Returns (post_id, n_comments).

        Takes the entire :class:`CrawlResult` so the data-quality notes travel
        with the data instead of being dropped on the floor - the report has a
        column for them, and a report that cannot be rebuilt from the database
        is not really stored.
        """
        post_id = await self.upsert_post(result.post)
        await self.record_metric_snapshot(post_id, job_id, result.post.metrics)
        written = await self.upsert_comments(post_id, result.comments)
        await self.link_job_post(
            job_id,
            post_id,
            rank,
            notes=result.notes,
            comments_collected=written,
            comments_truncated=result.comments_truncated,
        )
        return post_id, written


def _media_to_dict(item: Any) -> dict[str, Any]:
    return {
        "kind": item.kind,
        "url": item.url,
        "thumbnail_url": item.thumbnail_url,
        "width": item.width,
        "height": item.height,
    }


__all__ = ["CrawlRepository"]
