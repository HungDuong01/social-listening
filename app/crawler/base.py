"""The contract every platform adapter implements.

Shared mechanics (navigation with retry, throttling, cancellation, scrolling,
JSON interception) live here so an adapter only has to describe what is
genuinely platform-specific: URLs, response shapes, and field names.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Awaitable, Callable, ClassVar
from urllib.parse import urlparse

from playwright.async_api import BrowserContext
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Response

from app.core.config import Settings, get_settings
from app.core.enums import Platform, SearchFilter
from app.core.exceptions import JobCancelledError, NavigationError, UnknownPlatformError
from app.core.logging import get_logger
from app.core.utils import jitter_sleep, retry_async
from app.crawler.document import extract_inline_json
from app.crawler.harvester import NetworkHarvester
from app.crawler.ratelimit import RateLimiter
from app.crawler.scroller import ScrollPolicy
from app.domain.entities import CrawlResult, PostRef

ProgressCallback = Callable[[str], Awaitable[None]]

# Chromium surfaces an HTTP error status on a main-frame navigation as one of
# these net errors. They mean "the server turned this request away", which for
# a crawler almost always means rate limiting.
_THROTTLE_MARKERS = (
    "ERR_HTTP_RESPONSE_CODE_FAILURE",
    "ERR_TOO_MANY_REQUESTS",
    "ERR_BLOCKED_BY_RESPONSE",
    "ERR_BLOCKED_BY_CLIENT",
)


def is_throttle_error(error: BaseException) -> bool:
    """True when the platform turned the request away for going too fast."""
    message = str(error)
    return any(marker in message for marker in _THROTTLE_MARKERS)


@dataclass(slots=True)
class CrawlOptions:
    max_posts: int = 40
    max_comments_per_post: int = 200
    include_comments: bool = True
    search_filter: SearchFilter = SearchFilter.TOP


class CrawlContext:
    """Per-(job, platform) execution state handed to an adapter."""

    def __init__(
        self,
        *,
        job_id: int,
        platform: Platform,
        keyword: str,
        options: CrawlOptions,
        cancel_event: asyncio.Event,
        rate_limiter: RateLimiter,
        settings: Settings | None = None,
        progress: ProgressCallback | None = None,
    ) -> None:
        self.job_id = job_id
        self.platform = platform
        self.keyword = keyword
        self.options = options
        self.settings = settings or get_settings()
        self.logger = get_logger(f"crawler.{platform.value}")
        self._cancel_event = cancel_event
        self._rate_limiter = rate_limiter
        self._progress = progress

    # ------------------------------------------------------------ cancellation

    @property
    def cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise JobCancelledError(f"job {self.job_id} cancelled")

    # -------------------------------------------------------------- utilities

    async def throttle(self) -> None:
        self.raise_if_cancelled()
        await self._rate_limiter.acquire()

    def penalise(self, seconds: float) -> float:
        """Slow every later request down after the platform pushed back."""
        return self._rate_limiter.penalise(seconds)

    def relax(self) -> None:
        self._rate_limiter.relax()

    async def report(self, message: str) -> None:
        self.logger.info("[job %s] %s", self.job_id, message)
        if self._progress is not None:
            await self._progress(message)

    async def pause_between_posts(self) -> None:
        await jitter_sleep(self.settings.post_delay_min_ms, self.settings.post_delay_max_ms)

    def scroll_policy(self, *, deadline_s: float | None = None) -> ScrollPolicy:
        return ScrollPolicy(
            max_scrolls=self.settings.max_scrolls,
            idle_rounds=self.settings.scroll_idle_rounds,
            min_delay_ms=self.settings.scroll_delay_min_ms,
            max_delay_ms=self.settings.scroll_delay_max_ms,
            deadline_s=deadline_s,
        )


class PlatformAdapter(ABC):
    """Base class for platform-specific crawling logic."""

    platform: ClassVar[Platform]
    display_name: ClassVar[str]
    home_url: ClassVar[str]
    login_url: ClassVar[str]
    network_patterns: ClassVar[tuple[str, ...]] = ()
    # GraphQL operations whose payloads must never be treated as replies.
    excluded_operations: ClassVar[tuple[str, ...]] = ()
    requires_login: ClassVar[bool] = True
    # Some sites turn away a headless browser outright (the UA carries a
    # "HeadlessChrome" token). Those adapters ask for a visible window even when
    # the global default is headless, otherwise every crawl fails silently.
    prefers_headed: ClassVar[bool] = False
    # Requests per minute for this platform; None falls back to the global
    # setting. Sites differ wildly in how fast they will tolerate being read.
    rate_per_minute: ClassVar[int | None] = None
    # How many requests may fire back to back before the rate limit bites.
    # The bucket starts full, so a burst of 5 means the first five page loads
    # ignore the rate limit entirely - which is enough to trip a strict site
    # before throttling has had any effect.
    rate_burst: ClassVar[int | None] = None
    # ALL of these must be present for the session to count as live.
    session_cookie_names: ClassVar[tuple[str, ...]] = ()
    # Any of these means the account is mid-challenge, not signed in.
    blocking_cookie_names: ClassVar[tuple[str, ...]] = ()
    cookie_urls: ClassVar[tuple[str, ...]] = ()
    # URL paths that mean "not signed in yet", whatever the cookies say.
    interstitial_markers: ClassVar[tuple[str, ...]] = (
        "/login",
        "/checkpoint",
        "/challenge",
        "/two_step",
        "/two_factor",
        "/recover",
        "/accounts/login",
    )
    # Substrings that mark an inline SSR blob as worth parsing.
    inline_json_keywords: ClassVar[tuple[str, ...]] = ()

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.logger = get_logger(f"adapter.{self.platform.value}")

    # ---------------------------------------------------------------- contract

    @abstractmethod
    def build_search_url(self, keyword: str, options: CrawlOptions) -> str:
        """Map a keyword onto the platform's own search page."""

    @abstractmethod
    async def is_authenticated(self, page: Page) -> bool:
        """True when the current context can see logged-in content."""

    @abstractmethod
    async def collect_post_refs(self, page: Page, ctx: CrawlContext) -> list[PostRef]:
        """Open the search page and gather the posts it lists."""

    @abstractmethod
    async def fetch_post(self, page: Page, ref: PostRef, ctx: CrawlContext) -> CrawlResult:
        """Open one post and extract it together with its comments."""

    # ----------------------------------------------------------------- helpers

    async def has_live_session(self, context: BrowserContext) -> bool:
        """Cookie-only session probe.

        Used while the user is completing the interactive login: unlike
        :meth:`is_authenticated` it never navigates, so polling cannot disturb
        a half-finished form or challenge.

        **Every** name in ``session_cookie_names`` must be present. Requiring
        only one of them is what made Facebook logins loop: ``c_user`` (the
        account id) is set early, while the challenge is still running and the
        real session token ``xs`` does not exist yet. Declaring victory there
        stored a half-finished session and closed the window mid-flow.
        """
        if not self.session_cookie_names:
            return False
        urls = list(self.cookie_urls or (self.home_url,))
        try:
            cookies = await context.cookies(urls)
        except PlaywrightError:  # pragma: no cover
            return False

        present = {cookie["name"] for cookie in cookies if cookie.get("value")}
        if any(name in present for name in self.blocking_cookie_names):
            return False
        return all(name in present for name in self.session_cookie_names)

    async def confirm_session(self, context: BrowserContext) -> tuple[bool, str]:
        """Authoritative check: load the site and see whether it lets us stay.

        Cookies alone lie during a challenge. Only a real page load proves the
        platform accepts the session, so this runs before anything is saved.
        The probe uses its own page so the window the person is working in is
        left untouched.
        """
        page = await context.new_page()
        try:
            await page.goto(self.home_url, wait_until="domcontentloaded")
            await jitter_sleep(1_200, 2_000)

            landed = urlparse(page.url).path.lower()
            for marker in self.interstitial_markers:
                if marker in landed:
                    return False, f"nền tảng chuyển hướng tới {landed}"

            if not await self.has_live_session(context):
                return False, "thiếu cookie phiên hợp lệ sau khi tải trang"
            return True, page.url
        except PlaywrightError as error:
            return False, str(error).splitlines()[0]
        finally:
            try:
                await page.close()
            except PlaywrightError:  # pragma: no cover
                pass

    async def inline_documents(self, page: Page) -> list[object]:
        """JSON the server embedded in the page markup.

        Signed-out pages are rendered server-side and make no API calls, so
        this is the only place exact counters exist for them.
        """
        if not self.inline_json_keywords:
            return []
        return await extract_inline_json(page, keywords=self.inline_json_keywords)

    def make_harvester(self) -> NetworkHarvester:
        return NetworkHarvester(
            url_patterns=self.network_patterns,
            max_payloads=self.settings.max_payloads_per_page,
            max_body_bytes=self.settings.max_body_bytes,
        )

    async def navigate(
        self,
        page: Page,
        url: str,
        ctx: CrawlContext,
        *,
        wait_until: str = "domcontentloaded",
        settle_ms: tuple[int, int] = (1_200, 2_600),
        referer: str | None = None,
    ) -> Response | None:
        """Navigate with throttling, retries and a short settle pause.

        The settle pause matters: these pages render after the first XHR
        resolves, so reading the DOM the instant navigation returns yields an
        empty shell.

        ``referer`` should be passed when the crawler really did arrive from
        another page - a post opened from a search results page, say. Sending
        the header a browser would have sent keeps the request honest.
        """
        await ctx.throttle()

        async def _attempt() -> Response | None:
            return await page.goto(url, wait_until=wait_until, referer=referer)

        def _on_retry(attempt: int, error: BaseException) -> None:
            if is_throttle_error(error):
                self.logger.warning(
                    "%s answered with an error status for %s - backing off "
                    "(attempt %s). This is rate limiting, not a bad URL.",
                    self.platform.value,
                    url,
                    attempt,
                )
            else:
                self.logger.warning("Navigation retry %s for %s: %s", attempt, url, error)

        def _delay_for(attempt: int, error: BaseException) -> float:
            # A throttled response needs a real pause; retrying it on the
            # short schedule just deepens the throttling.
            return 20.0 * attempt if is_throttle_error(error) else min(10.0, 2.0 * 2 ** (attempt - 1))

        try:
            response = await retry_async(
                _attempt,
                attempts=3,
                retry_on=(PlaywrightError,),
                on_retry=_on_retry,
                delay_for=_delay_for,
            )
        except PlaywrightError as error:
            if is_throttle_error(error):
                # Measured on TikTok: a throttled run recovers fully after
                # roughly a minute of quiet, but only if the crawl actually
                # goes quiet. Holding the *next* post back is what breaks the
                # cascade - retrying this one harder does not.
                cooldown = ctx.penalise(45.0)
                self.logger.warning(
                    "%s is throttling - pausing this platform for %.0fs before the next post",
                    self.platform.value,
                    cooldown,
                )
            raise NavigationError(f"Could not open {url}: {error}") from error

        ctx.relax()

        await jitter_sleep(*settle_ms)
        ctx.raise_if_cancelled()
        return response

    async def count_links(self, page: Page, selector: str) -> int:
        try:
            return await page.locator(selector).count()
        except PlaywrightError:  # pragma: no cover - transient during navigation
            return 0

    async def dismiss_overlays(self, page: Page, selectors: tuple[str, ...]) -> None:
        """Close cookie / "continue as guest" dialogs that block scrolling."""
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() and await locator.is_visible():
                    await locator.click(timeout=2_000)
                    await jitter_sleep(400, 900)
            except PlaywrightError:
                continue


# --------------------------------------------------------------------- registry

_REGISTRY: dict[Platform, type[PlatformAdapter]] = {}


def register_adapter(adapter_class: type[PlatformAdapter]) -> type[PlatformAdapter]:
    _REGISTRY[adapter_class.platform] = adapter_class
    return adapter_class


def get_adapter(platform: Platform, settings: Settings | None = None) -> PlatformAdapter:
    adapter_class = _REGISTRY.get(platform)
    if adapter_class is None:
        raise UnknownPlatformError(f"No adapter registered for {platform.value}")
    return adapter_class(settings)


def registered_platforms() -> list[Platform]:
    return sorted(_REGISTRY.keys(), key=lambda platform: platform.value)


def adapter_descriptors() -> list[dict[str, str | bool]]:
    return [
        {
            "platform": platform.value,
            "label": adapter_class.display_name,
            "login_url": adapter_class.login_url,
            "requires_login": adapter_class.requires_login,
        }
        for platform, adapter_class in sorted(_REGISTRY.items(), key=lambda item: item[0].value)
    ]
