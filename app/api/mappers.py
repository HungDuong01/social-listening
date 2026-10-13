"""ORM -> API model conversions, kept out of the route handlers."""

from __future__ import annotations

from app.api.schemas import CommentOut, JobOut, JobTargetOut, MetricsOut, PostOut
from app.db.models import Comment, CrawlJob, JobTarget, Post


def post_to_out(post: Post) -> PostOut:
    author = post.author
    return PostOut(
        id=post.id,
        platform=post.platform,
        external_id=post.external_id,
        url=post.url,
        account=author.username if author else "",
        display_name=author.display_name if author else None,
        content=post.content or "",
        posted_at=post.posted_at,
        metrics=MetricsOut(
            likes=post.likes,
            comments=post.comments_count,
            reposts=post.reposts,
            shares=post.shares,
            views=post.views,
            saves=post.saves,
            source=post.metrics_source,
            approximate=post.metrics_approximate,
        ),
        hashtags=list(post.hashtags or []),
        media=list(post.media or []),
        comments_collected=post.comments_collected,
        first_seen_at=post.first_seen_at,
        last_seen_at=post.last_seen_at,
    )


def comment_to_out(comment: Comment) -> CommentOut:
    return CommentOut(
        id=comment.id,
        external_id=comment.external_id,
        author=comment.author.username if comment.author else "",
        content=comment.content or "",
        like_count=comment.like_count,
        reply_count=comment.reply_count,
        depth=comment.depth,
        is_author_reply=comment.is_author_reply,
        posted_at=comment.posted_at,
    )


def target_to_out(target: JobTarget) -> JobTargetOut:
    return JobTargetOut.model_validate(target)


def job_to_out(job: CrawlJob, targets: list[JobTarget]) -> JobOut:
    return JobOut(
        id=job.id,
        keyword=job.keyword,
        platforms=list(job.platforms or []),
        search_filter=job.search_filter,
        max_posts=job.max_posts,
        max_comments_per_post=job.max_comments_per_post,
        include_comments=job.include_comments,
        status=job.status,
        error=job.error,
        posts_found=job.posts_found,
        posts_crawled=job.posts_crawled,
        comments_crawled=job.comments_crawled,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        targets=[target_to_out(target) for target in targets],
    )
