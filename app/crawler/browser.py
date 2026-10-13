"""Playwright lifecycle and per-platform browser contexts."""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

from app.core.config import Settings, get_settings
from app.core.enums import Platform
from app.core.exceptions import BrowserNotReadyError
from app.core.logging import get_logger

logger = get_logger(__name__)


class BrowserManager:
    """Owns the Playwright driver and hands out configured contexts.

    Browsers are launched lazily and cached per headless flag: crawling runs
    headless, while the one-off login flow needs a visible window.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._playwright: Playwright | None = None
        self._browsers: dict[bool, Browser] = {}
        self._launch_lock = asyncio.Lock()
        self._state_locks: dict[str, asyncio.Lock] = {}
        self._profile_locks: dict[str, asyncio.Lock] = {}
        self._started = False

    # -------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self._started:
            return
        self._playwright = await async_playwright().start()
        self._started = True
        logger.info("Playwright driver started")

    async def stop(self) -> None:
        for headless, browser in list(self._browsers.items()):
            try:
                await browser.close()
            except Exception as error:  # pragma: no cover
                logger.warning("Failed to close browser (headless=%s): %s", headless, error)
        self._browsers.clear()

        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as error:  # pragma: no cover
                logger.warning("Failed to stop Playwright: %s", error)
        self._playwright = None
        self._started = False
        logger.info("Playwright driver stopped")

    async def _acquire_browser(self, headless: bool) -> Browser:
        if not self._started or self._playwright is None:
            raise BrowserNotReadyError("BrowserManager.start() was never awaited")

        async with self._launch_lock:
            browser = self._browsers.get(headless)
            if browser is not None and browser.is_connected():
                return browser

            logger.info("Launching Chromium (headless=%s)", headless)
            options: dict[str, object] = {
                "headless": headless,
                "slow_mo": self._settings.slow_mo_ms or 0,
                "args": [
                    "--disable-dev-shm-usage",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            }
            channel = self._settings.chromium_channel.strip()
            try:
                browser = await self._playwright.chromium.launch(
                    **({"channel": channel} | options) if channel else options
                )
            except PlaywrightError as error:
                if not channel:
                    raise
                logger.warning(
                    "Chromium channel %r unavailable (%s) - falling back to the bundled build",
                    channel,
                    str(error).splitlines()[0],
                )
                browser = await self._playwright.chromium.launch(**options)

            self._browsers[headless] = browser
            return browser

    # --------------------------------------------------------------- contexts

    def _context_options(self, headless: bool) -> dict[str, object]:
        """Context options that stay consistent with the real browser.

        The user agent is only sent when explicitly configured: Playwright
        overrides the UA header alone and leaves Sec-CH-UA client hints
        reporting the true build, so a pinned UA makes every request
        contradict itself.

        A headed window also keeps its natural size - forcing a viewport on a
        visible window makes the reported size disagree with the real one.
        """
        options: dict[str, object] = {
            "locale": self._settings.locale,
            "timezone_id": self._settings.timezone,
        }
        if self._settings.user_agent.strip():
            options["user_agent"] = self._settings.user_agent.strip()
        if headless:
            options["viewport"] = {
                "width": self._settings.viewport_width,
                "height": self._settings.viewport_height,
            }
        else:
            options["no_viewport"] = True
        return options

    def _profile_lock(self, platform: Platform) -> asyncio.Lock:
        return self._profile_locks.setdefault(platform.value, asyncio.Lock())

    @asynccontextmanager
    async def session(
        self,
        platform: Platform,
        *,
        headless: bool | None = None,
        persist: bool = True,
    ) -> AsyncIterator[BrowserContext]:
        """Preferred entry point: a persistent profile when one is enabled.

        Falls back to a throwaway context if the profile cannot be opened -
        typically because another window of this app already holds its lock.
        """
        if not self._settings.use_persistent_profile:
            async with self.context(platform, headless=headless, persist=persist) as context:
                yield context
            return

        lock = self._profile_lock(platform)
        if lock.locked():
            logger.warning(
                "Profile for %s is busy - using a temporary context for this run",
                platform.value,
            )
            async with self.context(platform, headless=headless, persist=persist) as context:
                yield context
            return

        async with lock:
            # Opening is guarded, using is not: wrapping the yield in the same
            # try would swallow navigation errors raised by the caller's body
            # and silently re-run it against a throwaway context.
            context = await self._open_persistent(platform, headless)
            if context is not None:
                try:
                    yield context
                finally:
                    await _close_context_quietly(context, platform)
                return

            async with self.context(platform, headless=headless, persist=persist) as fallback:
                yield fallback

    async def _open_persistent(
        self, platform: Platform, headless: bool | None
    ) -> BrowserContext | None:
        """Open the on-disk profile, or return None if it cannot be opened.

        Cookies, localStorage and the trust token a login challenge issues all
        live in this profile, so a second attempt continues as the same browser
        instead of presenting itself as a new one.
        """
        try:
            return await self._launch_persistent(platform, headless)
        except PlaywrightError as error:
            logger.warning(
                "Could not open the %s profile (%s) - using a temporary context",
                platform.value,
                str(error).splitlines()[0],
            )
            return None

    @asynccontextmanager
    async def persistent_context(
        self, platform: Platform, *, headless: bool | None = None
    ) -> AsyncIterator[BrowserContext]:
        context = await self._launch_persistent(platform, headless)
        try:
            yield context
        finally:
            await _close_context_quietly(context, platform)

    async def _launch_persistent(
        self, platform: Platform, headless: bool | None
    ) -> BrowserContext:
        if not self._started or self._playwright is None:
            raise BrowserNotReadyError("BrowserManager.start() was never awaited")

        use_headless = self._settings.headless if headless is None else headless
        profile = self._settings.profile_dir(platform.value)
        profile.mkdir(parents=True, exist_ok=True)

        launch_options: dict[str, object] = {
            "user_data_dir": str(profile),
            "headless": use_headless,
            "slow_mo": self._settings.slow_mo_ms or 0,
            "args": [
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            **self._context_options(use_headless),
        }
        channel = self._settings.chromium_channel.strip()
        if channel:
            launch_options["channel"] = channel

        logger.info(
            "Opening persistent profile for %s (headless=%s)", platform.value, use_headless
        )
        try:
            context = await self._playwright.chromium.launch_persistent_context(**launch_options)
        except PlaywrightError:
            if not channel:
                raise
            launch_options.pop("channel", None)
            context = await self._playwright.chromium.launch_persistent_context(**launch_options)

        context.set_default_timeout(self._settings.nav_timeout_ms)
        context.set_default_navigation_timeout(self._settings.nav_timeout_ms)
        return context

    @asynccontextmanager
    async def context(
        self,
        platform: Platform,
        *,
        headless: bool | None = None,
        persist: bool = True,
    ) -> AsyncIterator[BrowserContext]:
        use_headless = self._settings.headless if headless is None else headless
        browser = await self._acquire_browser(use_headless)
        state_path = self._settings.storage_state_path(platform.value)
        options = self._context_options(use_headless)

        context: BrowserContext
        if state_path.exists():
            try:
                context = await browser.new_context(storage_state=str(state_path), **options)
            except Exception as error:
                logger.warning(
                    "Saved session for %s is unusable (%s) - starting a clean context",
                    platform.value,
                    error,
                )
                context = await browser.new_context(**options)
        else:
            context = await browser.new_context(**options)

        context.set_default_timeout(self._settings.nav_timeout_ms)
        context.set_default_navigation_timeout(self._settings.nav_timeout_ms)

        try:
            yield context
        finally:
            if persist:
                await self.save_state(context, platform)
            try:
                await context.close()
            except Exception as error:  # pragma: no cover
                logger.debug("Context close failed for %s: %s", platform.value, error)

    async def save_state(self, context: BrowserContext, platform: Platform) -> bool:
        """Persist cookies to disk atomically.

        Written to a temporary file first: a crash halfway through a direct
        write would leave a truncated session file and force a fresh login.
        """
        lock = self._state_locks.setdefault(platform.value, asyncio.Lock())
        target = self._settings.storage_state_path(platform.value)
        temporary = target.with_suffix(".json.tmp")

        async with lock:
            try:
                await context.storage_state(path=str(temporary))
                os.replace(temporary, target)
                return True
            except Exception as error:
                logger.warning("Could not save session for %s: %s", platform.value, error)
                _remove_quietly(temporary)
                return False

    def clear_state(self, platform: Platform) -> bool:
        """Forget a platform completely - cookie file *and* browser profile.

        Leaving the profile behind would keep the old identity alive, so the
        next login would silently resume the session the user asked to drop.
        """
        removed = False
        target = self._settings.storage_state_path(platform.value)
        if target.exists():
            _remove_quietly(target)
            removed = True

        profile = self._settings.profile_dir(platform.value)
        if profile.exists():
            shutil.rmtree(profile, ignore_errors=True)
            removed = True
        return removed

    def has_state(self, platform: Platform) -> bool:
        if self._settings.storage_state_path(platform.value).exists():
            return True
        # Confirmed directly against this project's own profiles: current
        # Chromium keeps cookies at Default/Network/Cookies, not
        # Default/Cookies. Checking only the old path meant a real,
        # populated profile with a working session was reported as
        # "missing" in the UI. Both are checked - older Chromium releases
        # used the plain path, and this should keep working if a future
        # build moves it again from either checked location.
        profile = self._settings.profile_dir(platform.value)
        return (profile / "Default" / "Network" / "Cookies").exists() or (
            profile / "Default" / "Cookies"
        ).exists()


async def _close_context_quietly(context: BrowserContext, platform: Platform) -> None:
    try:
        await context.close()
    except Exception as error:  # pragma: no cover
        logger.debug("Context close failed for %s: %s", platform.value, error)


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as error:  # pragma: no cover
        logger.debug("Could not remove %s: %s", path, error)
