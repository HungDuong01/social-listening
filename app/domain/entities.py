"""Platform-neutral shapes produced by adapters and consumed by persistence.

Adapters translate whatever a platform returns into these objects, so nothing
below the adapter layer needs to know that TikTok calls a like a "digg" or
that Threads calls a share a "reshare".
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from app.core.enums import MetricSource, Platform

METRIC_FIELDS: tuple[str, ...] = ("likes", "comments", "shares", "reposts", "views", "saves")


@dataclass(slots=True)
class AuthorRef:
    platform: Platform
    username: str
    external_id: str | None = None
    display_name: str | None = None
    avatar_url: str | None = None
    is_verified: bool | None = None
    follower_count: int | None = None

    @property
    def key(self) -> str:
        """Identity used for de-duplication.

        Usernames can be renamed while the numeric id cannot, so the id wins
        whenever the platform gave us one.
        """
        return self.external_id or self.username.lower()


@dataclass(slots=True)
class MediaItem:
    kind: str  # "image" | "video" | "unknown"
    url: str
    thumbnail_url: str | None = None
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True, slots=True)
class Metrics:
    """Engagement counters.

    ``None`` means "we did not learn this number" - either the platform has no
    such concept, or it hid the counter. It is *not* interchangeable with 0.
    """

    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    reposts: int | None = None
    views: int | None = None
    saves: int | None = None
    source: MetricSource = MetricSource.UNKNOWN
    approximate: bool = False

    def merged_with(self, fallback: "Metrics") -> "Metrics":
        """Fill gaps in ``self`` using ``fallback``; ``self`` always wins.

        Call as ``exact.merged_with(approximate)`` so precise API numbers are
        never overwritten by rounded DOM text.
        """
        values: dict[str, Any] = {}
        used_fallback = False
        for name in METRIC_FIELDS:
            mine = getattr(self, name)
            if mine is None:
                other = getattr(fallback, name)
                values[name] = other
                used_fallback = used_fallback or other is not None
            else:
                values[name] = mine

        if self.source == MetricSource.UNKNOWN:
            source = fallback.source
        elif used_fallback and fallback.source not in (self.source, MetricSource.UNKNOWN):
            source = MetricSource.MIXED
        else:
            source = self.source

        approximate = self.approximate or (used_fallback and fallback.approximate)
        return Metrics(**values, source=source, approximate=approximate)

    @property
    def is_empty(self) -> bool:
        return all(getattr(self, name) is None for name in METRIC_FIELDS)


@dataclass(slots=True)
class PostRef:
    """A post discovered on a search page, before it has been opened."""

    platform: Platform
    external_id: str
    url: str
    author_username: str | None = None
    rank: int | None = None
    preview_metrics: Metrics = field(default_factory=Metrics)
    raw: dict[str, Any] | None = None


@dataclass(slots=True)
class RawPost:
    platform: Platform
    external_id: str
    url: str
    author: AuthorRef
    content: str = ""
    shortcode: str | None = None
    posted_at: datetime | None = None
    time_label: str | None = None
    metrics: Metrics = field(default_factory=Metrics)
    media: list[MediaItem] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)
    language: str | None = None
    raw: dict[str, Any] | None = None

    def with_metrics(self, metrics: Metrics) -> "RawPost":
        return replace(self, metrics=metrics)


@dataclass(slots=True)
class RawComment:
    platform: Platform
    external_id: str
    post_external_id: str
    author: AuthorRef
    content: str = ""
    parent_external_id: str | None = None
    posted_at: datetime | None = None
    time_label: str | None = None
    like_count: int | None = None
    reply_count: int | None = None
    depth: int = 1
    is_author_reply: bool = False
    raw: dict[str, Any] | None = None


@dataclass(slots=True)
class CrawlResult:
    """Everything harvested from one post page."""

    post: RawPost
    comments: list[RawComment] = field(default_factory=list)
    comments_truncated: bool = False
    notes: list[str] = field(default_factory=list)
