"""One-time interactive login per platform, then reuse of the saved session.

The crawler never handles credentials: a real browser window is opened, the
person signs in themselves, and only the resulting cookie jar is stored.
Polling during that window is cookie-based so it cannot navigate away from a
half-completed form.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from playwright.async_api import Error as PlaywrightError

from app.core.config import Settings, get_settings
from app.core.enums import Platform, SessionStatus
from app.core.logging import get_logger
from app.core.utils import utcnow
from app.crawler import BrowserManager, get_adapter

logger = get_logger(__name__)

_POLL_SECONDS = 2.0
# A challenge can leave cookies looking valid for a moment before the platform
# bounces the session back. Requiring the same verdict twice keeps a
# mid-challenge state from being stored as a working login.
#
# confirm_session() opens a second tab and navigates it to the platform's
# home page - and doing that on a short, fixed cycle while the visible tab
# is mid-checkpoint reproduces exactly the pattern anti-automation looks
# for: a background tab navigating on its own, with no user input, in the
# same session as one being driven by a human. Measured live on Facebook -
# a multi-step "confirm you're not a robot" checkpoint flickers c_user/xs
# in and out for a while before it is genuinely done, and each flicker used
# to cost one of these background navigations every 4s; the checkpoint
# re-triggered on the visible tab several times in a row as a result. Both
# knobs below exist to cut that down: wait for the cheap, non-navigating
# cookie check to hold steady for a while before ever spending a
# navigation on it, and once confirming, space attempts much further
# apart than before.
_STABLE_BEFORE_CONFIRM_S = 15.0
_CONFIRM_INTERVAL = 25.0
_REQUIRED_CONFIRMATIONS = 2


@dataclass(slots=True)
class LoginState:
    status: SessionStatus
    message: str = ""
    updated_at: datetime = field(default_factory=utcnow)


class SessionService:
    def __init__(self, browser: BrowserManager, settings: Settings | None = None) -> None:
        self._browser = browser
        self._settings = settings or get_settings()
        self._states: dict[Platform, LoginState] = {}
        self._tasks: dict[Platform, asyncio.Task[None]] = {}

    # ------------------------------------------------------------- inspection

    def describe(self, platform: Platform) -> dict[str, Any]:
        path = self._settings.storage_state_path(platform.value)
        saved_at: str | None = None
        if path.exists():
            saved_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()

        state = self._states.get(platform)
        if state is not None and state.status is SessionStatus.LOGGING_IN:
            status = SessionStatus.LOGGING_IN
        elif path.exists():
            status = state.status if state and state.status is SessionStatus.VALID else SessionStatus.SAVED
        else:
            status = state.status if state else SessionStatus.MISSING

        return {
            "platform": platform.value,
            "status": status.value,
            "message": state.message if state else "",
            "saved_at": saved_at,
            "has_file": path.exists(),
        }

    def describe_all(self) -> list[dict[str, Any]]:
        return [self.describe(platform) for platform in Platform]

    # ------------------------------------------------------------------ login

    async def begin_login(self, platform: Platform) -> dict[str, Any]:
        existing = self._tasks.get(platform)
        if existing is not None and not existing.done():
            return self.describe(platform)

        self._set_state(
            platform,
            SessionStatus.LOGGING_IN,
            "A browser window is opening - sign in there, it saves automatically",
        )
        task = asyncio.create_task(self._login_flow(platform), name=f"login-{platform.value}")
        self._tasks[platform] = task
        return self.describe(platform)

    async def _login_flow(self, platform: Platform) -> None:
        adapter = get_adapter(platform, self._settings)
        try:
            async with self._browser.session(
                platform, headless=False, persist=False
            ) as context:
                page = await context.new_page()
                await page.goto(adapter.login_url, wait_until="domcontentloaded")

                deadline = time.monotonic() + self._settings.login_timeout_s
                confirmations = 0
                last_note = ""
                # How long has_live_session() has been continuously true - a
                # navigation-free, cookie-only check, so polling it often is
                # free. None means "not currently live".
                live_since: float | None = None

                while time.monotonic() < deadline:
                    window_closed = page.is_closed()

                    if await adapter.has_live_session(context):
                        now = time.monotonic()
                        if live_since is None:
                            live_since = now
                        if now - live_since < _STABLE_BEFORE_CONFIRM_S:
                            # Looks right this instant, but a multi-step
                            # checkpoint can flicker this exact state before
                            # it truly settles - wait it out without opening
                            # the confirmation tab yet.
                            await asyncio.sleep(_POLL_SECONDS)
                            continue

                        # Cookies have held steady for a while - but only a
                        # real page load proves the platform actually accepts
                        # the session.
                        confirmed, detail = await adapter.confirm_session(context)
                        if confirmed:
                            confirmations += 1
                            self._set_state(
                                platform,
                                SessionStatus.LOGGING_IN,
                                f"Đăng nhập xong - đang xác nhận "
                                f"({confirmations}/{_REQUIRED_CONFIRMATIONS})",
                            )
                            if confirmations >= _REQUIRED_CONFIRMATIONS:
                                break
                        else:
                            confirmations = 0
                            # The steady-looking cookies turned out not to be
                            # accepted - reset the stability clock instead of
                            # retrying the navigation on the short cycle.
                            live_since = None
                            if detail != last_note:
                                last_note = detail
                                self._set_state(
                                    platform,
                                    SessionStatus.LOGGING_IN,
                                    f"Chưa hoàn tất: {detail}",
                                )
                        await asyncio.sleep(_CONFIRM_INTERVAL)
                        continue

                    confirmations = 0
                    live_since = None
                    if window_closed:
                        self._set_state(
                            platform,
                            SessionStatus.ERROR,
                            "Cửa sổ đăng nhập bị đóng trước khi phiên hoàn tất",
                        )
                        return
                    await asyncio.sleep(_POLL_SECONDS)
                else:
                    self._set_state(
                        platform,
                        SessionStatus.ERROR,
                        f"Chưa có phiên hợp lệ sau {self._settings.login_timeout_s}s "
                        "- nếu vẫn đang xác minh, hãy hoàn tất rồi bấm Đăng nhập lại",
                    )
                    return

                # Saved explicitly rather than on context exit: an unsuccessful
                # attempt must never overwrite a previously working session.
                if await self._browser.save_state(context, platform):
                    self._set_state(platform, SessionStatus.VALID, "Session saved")
                    logger.info("Session stored for %s", platform.value)
                else:
                    self._set_state(platform, SessionStatus.ERROR, "Could not write session file")
        except asyncio.CancelledError:
            self._set_state(platform, SessionStatus.ERROR, "Login cancelled")
            raise
        except PlaywrightError as error:
            self._set_state(platform, SessionStatus.ERROR, f"Browser error: {error}")
        except Exception as error:  # noqa: BLE001
            logger.exception("Login flow failed for %s", platform.value)
            self._set_state(platform, SessionStatus.ERROR, str(error))

    # ------------------------------------------------------------- validation

    async def validate(self, platform: Platform) -> dict[str, Any]:
        if not self._browser.has_state(platform):
            self._set_state(platform, SessionStatus.MISSING, "No saved session")
            return self.describe(platform)

        adapter = get_adapter(platform, self._settings)
        try:
            async with self._browser.session(platform, headless=True, persist=False) as context:
                page = await context.new_page()
                try:
                    ok = await adapter.is_authenticated(page)
                finally:
                    await page.close()
        except Exception as error:  # noqa: BLE001
            self._set_state(platform, SessionStatus.ERROR, str(error))
            return self.describe(platform)

        if ok:
            self._set_state(platform, SessionStatus.VALID, "Session works")
        else:
            self._set_state(platform, SessionStatus.INVALID, "Saved session is no longer accepted")
        return self.describe(platform)

    def clear(self, platform: Platform) -> dict[str, Any]:
        removed = self._browser.clear_state(platform)
        self._states.pop(platform, None)
        self._set_state(
            platform,
            SessionStatus.MISSING,
            "Session deleted" if removed else "There was no saved session",
        )
        return self.describe(platform)

    async def shutdown(self) -> None:
        for platform, task in list(self._tasks.items()):
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            self._tasks.pop(platform, None)

    # ---------------------------------------------------------------- helpers

    def _set_state(self, platform: Platform, status: SessionStatus, message: str) -> None:
        self._states[platform] = LoginState(status=status, message=message)
