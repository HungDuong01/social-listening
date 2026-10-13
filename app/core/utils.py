"""Small, dependency-free helpers used across the crawler."""

from __future__ import annotations

import asyncio
import hashlib
import random
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Sequence, TypeVar

T = TypeVar("T")

_WHITESPACE_RE = re.compile(r"[ \t   ]+")
_HASHTAG_RE = re.compile(r"#([\wÀ-ỹ]+)", re.UNICODE)
_MENTION_RE = re.compile(r"@([A-Za-z0-9._]{2,40})")
_COMPACT_RE = re.compile(
    r"^(?P<number>\d[\d.,\s  ]*)\s*(?P<suffix>[A-Za-zÀ-ỹ]{0,6})$"
)

# Display suffixes -> multiplier. Covers English (K/M/B) and Vietnamese
# (N = nghin, Tr = trieu, T = ty) because the UI locale changes the wording.
_SUFFIX_MULTIPLIERS: dict[str, int] = {
    "": 1,
    "k": 1_000,
    "n": 1_000,
    "nghin": 1_000,
    "m": 1_000_000,
    "tr": 1_000_000,
    "trieu": 1_000_000,
    "b": 1_000_000_000,
    "t": 1_000_000_000,
    "ty": 1_000_000_000,
}


@dataclass(frozen=True, slots=True)
class ParsedNumber:
    value: int | None
    approximate: bool

    @property
    def found(self) -> bool:
        return self.value is not None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def normalize_text(text: str | None) -> str:
    if not text:
        return ""
    cleaned = text.replace(" ", " ").replace("​", "")
    cleaned = _WHITESPACE_RE.sub(" ", cleaned)
    return "\n".join(line.strip() for line in cleaned.splitlines()).strip()


def parse_compact_number(raw: str | int | float | None) -> ParsedNumber:
    """Parse a metric as shown in a social UI into an integer.

    ``"1.9K"`` -> 1900 (approximate), ``"1,234"`` -> 1234 (exact),
    ``"1,9 N"`` -> 1900 (approximate). Returns ``value=None`` when the input
    holds no number at all, which is deliberately different from ``0``:
    most platforms hide a zero counter entirely, so "absent" and "zero" are
    not the same fact.
    """
    if raw is None:
        return ParsedNumber(None, False)
    if isinstance(raw, bool):
        return ParsedNumber(None, False)
    if isinstance(raw, int):
        return ParsedNumber(raw, False)
    if isinstance(raw, float):
        return ParsedNumber(int(raw), False)

    text = raw.strip().replace(" ", " ").replace(" ", " ")
    if not text:
        return ParsedNumber(None, False)

    match = _COMPACT_RE.match(text)
    if not match:
        return ParsedNumber(None, False)

    suffix_key = strip_accents(match.group("suffix")).lower()
    if suffix_key not in _SUFFIX_MULTIPLIERS:
        return ParsedNumber(None, False)
    multiplier = _SUFFIX_MULTIPLIERS[suffix_key]

    digits = re.sub(r"[\s  ]", "", match.group("number"))
    normalized = _normalize_decimal_separators(digits, has_suffix=multiplier > 1)
    try:
        value = float(normalized)
    except ValueError:
        return ParsedNumber(None, False)

    return ParsedNumber(int(round(value * multiplier)), approximate=multiplier > 1)


def _normalize_decimal_separators(digits: str, *, has_suffix: bool) -> str:
    """Turn a localised number string into something ``float()`` accepts."""
    dot_count = digits.count(".")
    comma_count = digits.count(",")

    if dot_count and comma_count:
        # The separator that appears last is the decimal one.
        decimal_sep = "." if digits.rfind(".") > digits.rfind(",") else ","
        group_sep = "," if decimal_sep == "." else "."
        return digits.replace(group_sep, "").replace(decimal_sep, ".")

    sep = "." if dot_count else ("," if comma_count else "")
    if not sep:
        return digits

    count = dot_count or comma_count
    if count > 1:
        # "1.234.567" - repeated separators can only be grouping.
        return digits.replace(sep, "")

    head, _, tail = digits.partition(sep)
    if not has_suffix and len(tail) == 3 and head:
        # "1,234" with no suffix is thousands, not a fraction.
        return head + tail
    return f"{head}.{tail}" if tail else head


def dig(obj: Any, *path: str | int, default: Any = None) -> Any:
    """Safely walk nested dict/list structures."""
    current = obj
    for key in path:
        if isinstance(key, int):
            if not isinstance(current, (list, tuple)) or not -len(current) <= key < len(current):
                return default
            current = current[key]
        else:
            if not isinstance(current, dict) or key not in current:
                return default
            current = current[key]
    return current if current is not None else default


def first_not_none(*values: T | None) -> T | None:
    for value in values:
        if value is not None:
            return value
    return None


def extract_hashtags(text: str | None) -> list[str]:
    if not text:
        return []
    return _dedupe_preserving_order(tag.lower() for tag in _HASHTAG_RE.findall(text))


def extract_mentions(text: str | None) -> list[str]:
    if not text:
        return []
    return _dedupe_preserving_order(name.lower() for name in _MENTION_RE.findall(text))


def _dedupe_preserving_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            output.append(value)
    return output


def stable_id(*parts: str) -> str:
    payload = "|".join(part or "" for part in parts)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:24]


async def jitter_sleep(min_ms: int, max_ms: int) -> None:
    low, high = (min_ms, max_ms) if min_ms <= max_ms else (max_ms, min_ms)
    await asyncio.sleep(random.uniform(low, high) / 1000)


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
    retry_on: Sequence[type[BaseException]] = (Exception,),
    on_retry: Callable[[int, BaseException], None] | None = None,
    delay_for: Callable[[int, BaseException], float] | None = None,
) -> T:
    """Run ``operation`` with exponential backoff + jitter.

    ``delay_for(attempt, error)`` overrides the backoff when the right pause
    depends on *why* it failed - a rate-limited response needs a far longer
    wait than a dropped connection, and retrying it on the short schedule only
    makes the throttling worse.

    ``asyncio.CancelledError`` is never retried - cancellation must win.
    """
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except asyncio.CancelledError:
            raise
        except tuple(retry_on) as error:  # noqa: PERF203 - retry is the point
            last_error = error
            if attempt == attempts:
                break
            if on_retry:
                on_retry(attempt, error)
            if delay_for is not None:
                delay = delay_for(attempt, error)
            else:
                delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            await asyncio.sleep(delay * random.uniform(0.6, 1.4))
    assert last_error is not None
    raise last_error


def epoch_to_datetime(value: Any) -> datetime | None:
    """Convert a platform timestamp (seconds, millis or micros) to UTC."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    if number > 1e17:  # microseconds
        number /= 1_000_000
    elif number > 1e14:  # milliseconds
        number /= 1_000
    if number > 4_102_444_800:  # beyond year 2100 - not a timestamp
        return None
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
