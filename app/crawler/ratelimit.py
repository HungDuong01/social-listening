"""Token-bucket throttle so a crawl stays a polite, human-paced visitor."""

from __future__ import annotations

import asyncio
import time


class RateLimiter:
    """Allows ``per_minute`` operations with a small burst allowance."""

    def __init__(self, per_minute: int, burst: int = 5) -> None:
        self._enabled = per_minute > 0
        self._capacity = max(1, burst)
        self._refill_per_second = per_minute / 60 if self._enabled else 0.0
        self._tokens = float(self._capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()
        self._blocked_until = 0.0
        self._penalties = 0

    async def acquire(self, tokens: float = 1.0) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()

                # A cooldown outranks the token bucket: once the platform has
                # said "too fast", the next request must wait even if tokens
                # are available.
                if now < self._blocked_until:
                    wait_for = self._blocked_until - now
                else:
                    if not self._enabled:
                        return
                    elapsed = now - self._updated
                    self._updated = now
                    self._tokens = min(
                        self._capacity, self._tokens + elapsed * self._refill_per_second
                    )
                    if self._tokens >= tokens:
                        self._tokens -= tokens
                        return
                    wait_for = (tokens - self._tokens) / self._refill_per_second

            # Sleep outside the lock so other callers can refill concurrently.
            await asyncio.sleep(min(wait_for, 30.0))

    def penalise(self, seconds: float) -> float:
        """Hold every later request back after the platform pushed us away.

        Without this, a throttled post is retried a few times, fails, and the
        crawl immediately slams into the next one at full speed - which is how
        a single 403 turns into a run of them. Repeat offences lengthen the
        cooldown.
        """
        self._penalties += 1
        cooldown = seconds * self._penalties
        self._blocked_until = max(self._blocked_until, time.monotonic() + cooldown)
        self._tokens = 0.0
        return cooldown

    def relax(self) -> None:
        """A success means the pressure is off; stop escalating."""
        self._penalties = 0

    @property
    def cooling_down(self) -> bool:
        return time.monotonic() < self._blocked_until

    @property
    def enabled(self) -> bool:
        return self._enabled
