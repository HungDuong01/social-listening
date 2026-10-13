"""Runs one crawl job: search -> post refs -> post pages -> database.

Transactions are deliberately short-lived (one per persisted post) instead of
one long transaction per job. A crawl runs for minutes; holding a SQLite write
transaction open that whole time would block every read, so the API could not
show progress while work is happening.
"""

from __future__ import annotations

import asyncio

from playwright.async_api import Page

from app.core.config import Settings, get_settings
from app.core.enums import JobStatus, Platform, SearchFilter, TargetStatus
from app.core.exceptions import (
    AuthenticationRequiredError,
    JobCancelledError,
    PlatformBlockedError,
)
from app.core.logging import get_logger
from app.crawler import (
    BrowserManager,
    CrawlContext,
    CrawlOptions,
    PlatformAdapter,
    RateLimiter,
    get_adapter,
    is_throttle_error,
)
from app.db.repositories import CrawlRepository, JobRepository
from app.db.session import session_scope
from app.domain.entities import CrawlResult, PostRef

logger = get_logger(__name__)


class CrawlService:
    def __init__(self, browser: BrowserManager, settings: Settings | None = None) -> None:
        self._browser = browser
        self._settings = settings or get_settings()
        self._limiters: dict[Platform, RateLimiter] = {}

    def _limiter(self, adapter: PlatformAdapter) -> RateLimiter:
        limiter = self._limiters.get(adapter.platform)
        if limiter is None:
            per_minute = adapter.rate_per_minute or self._settings.rate_per_minute
            burst = adapter.rate_burst or self._settings.rate_burst
            limiter = RateLimiter(per_minute, burst)
            self._limiters[adapter.platform] = limiter
        return limiter

    # ------------------------------------------------------------------ public

    async def run_job(self, job_id: int, cancel_event: asyncio.Event) -> JobStatus:
        async with session_scope() as session:
            jobs = JobRepository(session)
            job = await jobs.get(job_id)
            if job is None:
                raise ValueError(f"Job {job_id} does not exist")

            keyword = job.keyword
            platforms = [Platform(value) for value in job.platforms]
            options = CrawlOptions(
                max_posts=job.max_posts,
                max_comments_per_post=job.max_comments_per_post,
                include_comments=job.include_comments,
                search_filter=SearchFilter(job.search_filter),
            )
            await jobs.mark_running(job_id)

        logger.info("Job %s started: %r across %s", job_id, keyword, [p.value for p in platforms])
        cancelled = False

        for platform in platforms:
            if cancel_event.is_set():
                cancelled = True
                break
            try:
                await self._run_platform(job_id, platform, keyword, options, cancel_event)
            except JobCancelledError:
                cancelled = True
                await self._finish_target(job_id, platform, TargetStatus.CANCELLED)
                break
            except AuthenticationRequiredError as error:
                logger.warning("Job %s: %s needs a session - %s", job_id, platform.value, error)
                await self._finish_target(
                    job_id,
                    platform,
                    TargetStatus.NEEDS_AUTH,
                    error=str(error),
                    message="Đăng nhập nền tảng này rồi chạy lại job",
                )
            except PlatformBlockedError as error:
                # Deliberately not NEEDS_AUTH: signing in cannot fix this, and
                # saying otherwise sends the user off to do useless work.
                logger.warning("Job %s: %s blocked us - %s", job_id, platform.value, error)
                await self._finish_target(
                    job_id,
                    platform,
                    TargetStatus.BLOCKED,
                    error=str(error),
                    message=error.remedy or "Nền tảng từ chối phiên này",
                )
            except Exception as error:  # noqa: BLE001 - one platform must not sink the job
                logger.exception("Job %s: %s failed", job_id, platform.value)
                await self._finish_target(
                    job_id, platform, TargetStatus.FAILED, error=f"{type(error).__name__}: {error}"
                )

        async with session_scope() as session:
            jobs = JobRepository(session)
            if cancelled:
                await jobs.cancel_unfinished_targets(job_id)
                status = JobStatus.CANCELLED
            else:
                status = await jobs.derive_job_status(job_id)
            await jobs.finish_job(job_id, status)

        logger.info("Job %s finished with status %s", job_id, status.value)
        return status

    # --------------------------------------------------------------- internals

    async def _run_platform(
        self,
        job_id: int,
        platform: Platform,
        keyword: str,
        options: CrawlOptions,
        cancel_event: asyncio.Event,
    ) -> None:
        adapter = get_adapter(platform, self._settings)
        search_url = adapter.build_search_url(keyword, options)

        async with session_scope() as session:
            await JobRepository(session).start_target(job_id, platform, search_url)

        context = CrawlContext(
            job_id=job_id,
            platform=platform,
            keyword=keyword,
            options=options,
            cancel_event=cancel_event,
            rate_limiter=self._limiter(adapter),
            settings=self._settings,
            progress=lambda message: self._publish_progress(job_id, platform, message),
        )

        # Adapters that a headless browser gets turned away from always run in a
        # visible window, whatever the global default says.
        headless = False if adapter.prefers_headed else None
        if adapter.prefers_headed and self._settings.headless:
            logger.info("%s requires a visible window - overriding headless", platform.value)

        # persist=False: the cookie jar is written back only after the session
        # proves authenticated. Saving unconditionally would store an anonymous
        # jar and then report it in the UI as a saved login.
        async with self._browser.session(
            platform, headless=headless, persist=False
        ) as browser_context:
            page = await browser_context.new_page()
            try:
                if adapter.requires_login and not await adapter.is_authenticated(page):
                    raise AuthenticationRequiredError(platform.value)

                await context.report(f"searching for {keyword!r}")
                refs = await adapter.collect_post_refs(page, context)
                refs = refs[: options.max_posts]

                async with session_scope() as session:
                    repository = JobRepository(session)
                    await repository.bump_target(job_id, platform, posts_found=len(refs))
                    await repository.bump_job(job_id, posts_found=len(refs))

                await context.report(f"found {len(refs)} posts - opening them one by one")
                await self._crawl_refs(job_id, platform, adapter, page, refs, context)
                # Refresh the stored cookies so a working session keeps living.
                await self._browser.save_state(browser_context, platform)
            finally:
                await _close_quietly(page)

        async with session_scope() as session:
            await JobRepository(session).finish_target(job_id, platform, TargetStatus.COMPLETED)

    async def _crawl_refs(
        self,
        job_id: int,
        platform: Platform,
        adapter: PlatformAdapter,
        page: Page,
        refs: list[PostRef],
        context: CrawlContext,
    ) -> None:
        total = len(refs)
        pending = list(refs)
        done = 0

        # Two passes. A post turned away for going too fast is not a broken
        # post - the same URL loads fine once the platform has cooled off, and
        # failing it outright would lose data to a condition that clears
        # itself. The cooldown applied when it failed has already elapsed by
        # the time the second pass reaches it.
        for attempt in (1, 2):
            if not pending:
                break
            if attempt == 2:
                await context.report(
                    f"thử lại {len(pending)} bài bị chặn tốc độ sau khi nghỉ"
                )

            deferred: list[PostRef] = []
            for ref in pending:
                context.raise_if_cancelled()
                try:
                    result = await adapter.fetch_post(page, ref, context)
                except (JobCancelledError, AuthenticationRequiredError):
                    raise
                except Exception as error:  # noqa: BLE001 - one bad post must not end the crawl
                    if attempt == 1 and is_throttle_error(error):
                        deferred.append(ref)
                        logger.info(
                            "Job %s: %s was throttled - queued for a second pass",
                            job_id,
                            ref.url,
                        )
                    else:
                        await self._record_post_failure(job_id, platform, ref, error)
                    continue

                done += 1
                comment_count = await self._persist_post(job_id, ref, result)
                await self._count_post(job_id, platform, comment_count)
                await context.report(
                    f"[{done}/{total}] {ref.url} - {comment_count} comments stored"
                )
                await context.pause_between_posts()

            pending = deferred

    async def _persist_post(self, job_id: int, ref: PostRef, result: CrawlResult) -> int:
        async with session_scope() as session:
            _, comment_count = await CrawlRepository(session).persist_result(
                job_id=job_id, result=result, rank=ref.rank
            )
        return comment_count

    async def _count_post(self, job_id: int, platform: Platform, comments: int) -> None:
        async with session_scope() as session:
            repository = JobRepository(session)
            await repository.bump_target(
                job_id, platform, posts_crawled=1, comments_crawled=comments
            )
            await repository.bump_job(job_id, posts_crawled=1, comments_crawled=comments)

    async def _record_post_failure(
        self, job_id: int, platform: Platform, ref: PostRef, error: BaseException
    ) -> None:
        logger.warning("Job %s: post %s failed - %s", job_id, ref.url, error)
        async with session_scope() as session:
            await JobRepository(session).bump_target(
                job_id,
                platform,
                posts_failed=1,
                # Surface why a post was skipped; a silent counter tells the
                # user nothing about whether it is worth retrying.
                message=f"bỏ qua {ref.external_id}: {str(error).splitlines()[0][:110]}",
            )

    async def _publish_progress(self, job_id: int, platform: Platform, message: str) -> None:
        async with session_scope() as session:
            await JobRepository(session).update_target(job_id, platform, message=message[:500])

    async def _finish_target(
        self,
        job_id: int,
        platform: Platform,
        status: TargetStatus,
        *,
        error: str | None = None,
        message: str | None = None,
    ) -> None:
        async with session_scope() as session:
            await JobRepository(session).finish_target(
                job_id, platform, status, error=error, message=message
            )


async def _close_quietly(page: Page) -> None:
    try:
        await page.close()
    except Exception as error:  # pragma: no cover
        logger.debug("Page close failed: %s", error)
