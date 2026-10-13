"""Generic infinite-scroll driver shared by every adapter."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from playwright.async_api import Page

from app.core.logging import get_logger
from app.core.utils import jitter_sleep

logger = get_logger(__name__)

CountItems = Callable[[], Awaitable[int]]
ShouldStop = Callable[[int], bool]
OnRound = Callable[[int, int], Awaitable[None]]


@dataclass(slots=True)
class ScrollPolicy:
    max_scrolls: int = 60
    idle_rounds: int = 3
    min_delay_ms: int = 900
    max_delay_ms: int = 2_100
    deadline_s: float | None = None


@dataclass(slots=True)
class ScrollOutcome:
    rounds: int
    items: int
    reason: str


async def infinite_scroll(
    page: Page,
    *,
    policy: ScrollPolicy,
    count_items: CountItems,
    should_stop: ShouldStop | None = None,
    on_round: OnRound | None = None,
) -> ScrollOutcome:
    """Scroll until the feed stops producing new items.

    Stops on whichever comes first: the caller's target, a run of rounds that
    added nothing, the scroll cap, or the deadline. Both the item count and the
    document height are tracked because virtualised feeds recycle DOM nodes and
    would otherwise look "idle" while still loading.
    """
    started = time.monotonic()
    seen = await count_items()
    last_height = await _document_height(page)
    idle = 0
    reason = "max_scrolls"
    rounds = 0

    for round_index in range(policy.max_scrolls):
        rounds = round_index + 1

        if should_stop is not None and should_stop(seen):
            reason = "target_reached"
            break
        if policy.deadline_s is not None and time.monotonic() - started > policy.deadline_s:
            reason = "deadline"
            break

        await page.evaluate("window.scrollBy(0, Math.round(window.innerHeight * 0.9));")
        await jitter_sleep(policy.min_delay_ms, policy.max_delay_ms)

        current = await count_items()
        height = await _document_height(page)

        if current > seen or height > last_height:
            idle = 0
        else:
            idle += 1

        seen = max(seen, current)
        last_height = max(last_height, height)

        if on_round is not None:
            await on_round(rounds, seen)

        if idle >= policy.idle_rounds:
            reason = "no_new_items"
            break

    logger.debug("Scroll finished after %s rounds (%s items, reason=%s)", rounds, seen, reason)
    return ScrollOutcome(rounds=rounds, items=seen, reason=reason)


async def _document_height(page: Page) -> int:
    try:
        value = await page.evaluate(
            "Math.max(document.body ? document.body.scrollHeight : 0,"
            " document.documentElement ? document.documentElement.scrollHeight : 0)"
        )
        return int(value or 0)
    except Exception:  # pragma: no cover - page may navigate mid-measure
        return 0
