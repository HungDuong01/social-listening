"""SQLAlchemy ORM model definitions.

Design notes
------------
* ``posts`` carries the *latest* metric values denormalised onto the row so the
  UI can list and sort without a correlated subquery, while
  ``post_metric_snapshots`` keeps the full history - engagement changes over
  time and a crawler that overwrites yesterday's numbers loses that signal.
* Author identity uses ``identity_key`` (numeric id when the platform exposes
  one, lowercased username otherwise) because SQLite treats NULLs as distinct
  and a nullable column can therefore never act as a unique key.
* ``comments.parent_external_id`` is stored alongside the resolved
  ``parent_id`` FK: replies frequently arrive before their parent, so the FK is
  linked in a second pass once the whole batch exists.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.core.enums import JobStatus, MetricSource, SearchFilter, TargetStatus
from app.core.utils import utcnow

# JSONB on PostgreSQL (binary, indexable, supports containment operators),
# plain JSON text elsewhere. Same Python-side type either way.
JSONType = JSON().with_variant(JSONB(), "postgresql")


class UtcDateTime(TypeDecorator):
    """Timezone-safe datetime for SQLite.

    SQLite stores no timezone, so a tz-aware value written through a plain
    ``DateTime`` comes back naive - and a naive UTC timestamp serialised to
    JSON is read by browsers as *local* time, silently shifting every
    displayed date. This normalises to UTC on write and re-attaches UTC on
    read, so the whole stack speaks one timezone.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:  # noqa: ANN001
        if value is None:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:  # noqa: ANN001
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


def _now_column(**kwargs) -> Mapped[datetime]:
    return mapped_column(UtcDateTime, default=utcnow, **kwargs)


class CrawlJob(Base):
    __tablename__ = "crawl_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    keyword: Mapped[str] = mapped_column(String(500), index=True)
    platforms: Mapped[list] = mapped_column(JSONType, default=list)
    search_filter: Mapped[str] = mapped_column(String(20), default=SearchFilter.TOP.value)
    max_posts: Mapped[int] = mapped_column(Integer, default=40)
    max_comments_per_post: Mapped[int] = mapped_column(Integer, default=200)
    include_comments: Mapped[bool] = mapped_column(Boolean, default=True)

    status: Mapped[str] = mapped_column(String(20), default=JobStatus.PENDING.value, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    posts_found: Mapped[int] = mapped_column(Integer, default=0)
    posts_crawled: Mapped[int] = mapped_column(Integer, default=0)
    comments_crawled: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = _now_column(index=True)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    targets: Mapped[list["JobTarget"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", lazy="selectin"
    )


class JobTarget(Base):
    """One row per (job, platform) - each platform succeeds or fails alone."""

    __tablename__ = "job_targets"
    __table_args__ = (UniqueConstraint("job_id", "platform", name="uq_job_target"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("crawl_jobs.id", ondelete="CASCADE"), index=True)
    platform: Mapped[str] = mapped_column(String(20), index=True)
    status: Mapped[str] = mapped_column(String(20), default=TargetStatus.PENDING.value)
    search_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    posts_found: Mapped[int] = mapped_column(Integer, default=0)
    posts_crawled: Mapped[int] = mapped_column(Integer, default=0)
    posts_failed: Mapped[int] = mapped_column(Integer, default=0)
    comments_crawled: Mapped[int] = mapped_column(Integer, default=0)

    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    job: Mapped[CrawlJob] = relationship(back_populates="targets")


class Author(Base):
    __tablename__ = "authors"
    __table_args__ = (UniqueConstraint("platform", "identity_key", name="uq_author_identity"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(20), index=True)
    identity_key: Mapped[str] = mapped_column(String(255))
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    username: Mapped[str] = mapped_column(String(255), index=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    follower_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    first_seen_at: Mapped[datetime] = _now_column()
    last_seen_at: Mapped[datetime] = _now_column()


class Post(Base):
    __tablename__ = "posts"
    __table_args__ = (
        UniqueConstraint("platform", "external_id", name="uq_post_identity"),
        Index("ix_post_platform_posted", "platform", "posted_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(20), index=True)
    external_id: Mapped[str] = mapped_column(String(255))
    shortcode: Mapped[str | None] = mapped_column(String(255), nullable=True)
    url: Mapped[str] = mapped_column(Text)
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("authors.id", ondelete="SET NULL"), nullable=True, index=True
    )

    content: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    time_label: Mapped[str | None] = mapped_column(String(64), nullable=True)

    media: Mapped[list] = mapped_column(JSONType, default=list)
    hashtags: Mapped[list] = mapped_column(JSONType, default=list)
    mentions: Mapped[list] = mapped_column(JSONType, default=list)
    raw: Mapped[dict | None] = mapped_column(JSONType, nullable=True)

    # Latest known metrics (history lives in post_metric_snapshots).
    likes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comments_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shares: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reposts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    views: Mapped[int | None] = mapped_column(Integer, nullable=True)
    saves: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metrics_source: Mapped[str] = mapped_column(String(16), default=MetricSource.UNKNOWN.value)
    metrics_approximate: Mapped[bool] = mapped_column(Boolean, default=False)

    comments_collected: Mapped[int] = mapped_column(Integer, default=0)
    first_seen_at: Mapped[datetime] = _now_column()
    last_seen_at: Mapped[datetime] = _now_column(index=True)

    author: Mapped[Author | None] = relationship(lazy="joined")


class PostMetricSnapshot(Base):
    __tablename__ = "post_metric_snapshots"
    __table_args__ = (UniqueConstraint("post_id", "job_id", name="uq_snapshot_post_job"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("crawl_jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )

    likes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comments_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shares: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reposts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    views: Mapped[int | None] = mapped_column(Integer, nullable=True)
    saves: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default=MetricSource.UNKNOWN.value)
    approximate: Mapped[bool] = mapped_column(Boolean, default=False)
    captured_at: Mapped[datetime] = _now_column(index=True)


class Comment(Base):
    __tablename__ = "comments"
    __table_args__ = (
        UniqueConstraint("platform", "external_id", name="uq_comment_identity"),
        Index("ix_comment_post_depth", "post_id", "depth"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(20), index=True)
    external_id: Mapped[str] = mapped_column(String(255))
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("comments.id", ondelete="SET NULL"), nullable=True
    )
    parent_external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("authors.id", ondelete="SET NULL"), nullable=True, index=True
    )

    content: Mapped[str] = mapped_column(Text, default="")
    posted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    time_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    like_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reply_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    depth: Mapped[int] = mapped_column(Integer, default=1)
    is_author_reply: Mapped[bool] = mapped_column(Boolean, default=False)
    raw: Mapped[dict | None] = mapped_column(JSONType, nullable=True)

    first_seen_at: Mapped[datetime] = _now_column()
    last_seen_at: Mapped[datetime] = _now_column()

    author: Mapped[Author | None] = relationship(lazy="joined")


class JobPost(Base):
    """Many-to-many: the same post can surface for several keywords.

    Also the per-run observation record. ``notes`` and ``comments_collected``
    belong here rather than on ``posts`` because they describe *this crawl* of
    the post: the same post read again tomorrow may collect a different number
    of comments and carry different caveats, and a report is always rendered
    for one job.
    """

    __tablename__ = "job_posts"

    job_id: Mapped[int] = mapped_column(
        ForeignKey("crawl_jobs.id", ondelete="CASCADE"), primary_key=True
    )
    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), primary_key=True
    )
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Data-quality caveats produced while crawling, e.g. "collected 46/47
    # comments". The report has a Note column for exactly this, and without
    # storing them the report cannot be rebuilt from the database.
    notes: Mapped[list] = mapped_column(JSONType, default=list)
    comments_collected: Mapped[int] = mapped_column(Integer, default=0)
    comments_truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    found_at: Mapped[datetime] = _now_column()
