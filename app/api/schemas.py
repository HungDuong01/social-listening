"""Request / response models for the HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.enums import Platform, SearchFilter


class CreateJobRequest(BaseModel):
    keyword: str = Field(min_length=1, max_length=300)
    platforms: list[Platform] = Field(min_length=1)
    search_filter: SearchFilter = SearchFilter.TOP
    max_posts: int | None = Field(default=None, ge=1, le=500)
    max_comments_per_post: int | None = Field(default=None, ge=0, le=2_000)
    include_comments: bool = True

    @field_validator("keyword")
    @classmethod
    def _strip_keyword(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("keyword must not be blank")
        return cleaned

    @field_validator("platforms")
    @classmethod
    def _unique_platforms(cls, value: list[Platform]) -> list[Platform]:
        seen: list[Platform] = []
        for platform in value:
            if platform not in seen:
                seen.append(platform)
        return seen


class JobTargetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    platform: str
    status: str
    search_url: str | None = None
    posts_found: int = 0
    posts_crawled: int = 0
    posts_failed: int = 0
    comments_crawled: int = 0
    message: str | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    keyword: str
    platforms: list[str]
    search_filter: str
    max_posts: int
    max_comments_per_post: int
    include_comments: bool
    status: str
    error: str | None = None
    posts_found: int = 0
    posts_crawled: int = 0
    comments_crawled: int = 0
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    targets: list[JobTargetOut] = Field(default_factory=list)


class JobListOut(BaseModel):
    items: list[JobOut]
    total: int


class MetricsOut(BaseModel):
    likes: int | None = None
    comments: int | None = None
    reposts: int | None = None
    shares: int | None = None
    views: int | None = None
    saves: int | None = None
    source: str = "unknown"
    approximate: bool = False


class PostOut(BaseModel):
    id: int
    platform: str
    external_id: str
    url: str
    account: str
    display_name: str | None = None
    content: str
    posted_at: datetime | None = None
    metrics: MetricsOut
    hashtags: list[str] = Field(default_factory=list)
    media: list[dict[str, Any]] = Field(default_factory=list)
    comments_collected: int = 0
    first_seen_at: datetime
    last_seen_at: datetime


class PostListOut(BaseModel):
    items: list[PostOut]
    total: int
    limit: int
    offset: int


class CommentOut(BaseModel):
    id: int
    external_id: str
    author: str
    content: str
    like_count: int | None = None
    reply_count: int | None = None
    depth: int
    is_author_reply: bool
    posted_at: datetime | None = None


class PostDetailOut(BaseModel):
    post: PostOut
    comments: list[CommentOut]


class PlatformOut(BaseModel):
    platform: str
    label: str
    login_url: str
    requires_login: bool


class SessionOut(BaseModel):
    platform: str
    status: str
    message: str = ""
    saved_at: str | None = None
    has_file: bool = False


class StatsOut(BaseModel):
    posts: int
    comments: int
    authors: int
    jobs: int
    by_platform: list[dict[str, Any]] = Field(default_factory=list)


class ExportRequest(BaseModel):
    format: str = Field(default="xlsx", pattern="^(xlsx|csv|json)$")
    job_id: int | None = None
    platform: Platform | None = None
    keyword: str | None = None
    limit: int = Field(default=5_000, ge=1, le=50_000)


class WorkerStatusOut(BaseModel):
    running: int | None = None
    queued: int = 0
    worker_alive: bool = False
    started_at: str | None = None
