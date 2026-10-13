"""Capture the JSON a page fetches for itself.

Rendered text rounds numbers ("1.9K") and hides zero counters entirely, so the
DOM can never tell "no likes" apart from "1,903 likes". The JSON the page
requests in the background carries exact integers, which is why interception -
not scraping - is the primary extraction strategy here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Sequence
from urllib.parse import parse_qs, urlparse

from playwright.async_api import Page, Response

from app.core.logging import get_logger

logger = get_logger(__name__)

# Anti-JSON-hijacking prefixes used by Meta and Google style endpoints.
_JSON_PREFIXES = ("for (;;);", "for(;;);", ")]}'", ")]}\"", "while(1);")

_OPERATION_KEYS = ("fb_api_req_friendly_name", "operationName", "doc_id")


@dataclass(slots=True)
class HarvestedPayload:
    url: str
    operation: str | None
    data: Any

    def operation_matches(self, needles: Sequence[str]) -> bool:
        if not self.operation:
            return False
        lowered = self.operation.lower()
        return any(needle.lower() in lowered for needle in needles)


class NetworkHarvester:
    """Buffers JSON bodies of responses whose URL matches ``url_patterns``.

    Deliberately a plain class, not a ``slots=True`` dataclass: Playwright
    stores bookkeeping for a bound event handler as an attribute on the
    handler's owner, which requires this object to have a ``__dict__``.
    """

    def __init__(
        self,
        url_patterns: Sequence[str],
        *,
        max_payloads: int = 400,
        max_body_bytes: int = 4_000_000,
    ) -> None:
        self.url_patterns = url_patterns
        self.max_payloads = max_payloads
        self.max_body_bytes = max_body_bytes
        self._compiled = [re.compile(pattern) for pattern in url_patterns]
        self._payloads: list[HarvestedPayload] = []
        self._attached_to: Page | None = None
        self._dropped = 0
        # Error statuses seen on matching URLs. A throttled XHR never produces
        # a payload, so without this the caller cannot tell "the feed ended"
        # from "the platform started refusing us".
        self._error_statuses: list[tuple[int, str]] = []

    # ------------------------------------------------------------- lifecycle

    def attach(self, page: Page) -> None:
        if self._attached_to is not None:
            self.detach()
        page.on("response", self._on_response)
        self._attached_to = page

    def detach(self) -> None:
        if self._attached_to is None:
            return
        try:
            self._attached_to.remove_listener("response", self._on_response)
        except Exception:  # pragma: no cover - page may already be closed
            pass
        self._attached_to = None

    def __enter__(self) -> "NetworkHarvester":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.detach()

    # ------------------------------------------------------------- collection

    async def _on_response(self, response: Response) -> None:
        try:
            url = response.url
            if not any(pattern.search(url) for pattern in self._compiled):
                return
            if len(self._payloads) >= self.max_payloads:
                self._dropped += 1
                return

            if response.status >= 400:
                self._error_statuses.append((response.status, url))
                return

            headers = await _safe_headers(response)
            content_type = headers.get("content-type", "")
            if content_type and not _looks_like_json(content_type):
                return
            length = headers.get("content-length")
            if length and length.isdigit() and int(length) > self.max_body_bytes:
                return

            body = await response.text()
            if not body or len(body) > self.max_body_bytes:
                return

            operation = _operation_name(response)
            for document in loads_multi(body):
                if len(self._payloads) >= self.max_payloads:
                    self._dropped += 1
                    break
                self._payloads.append(HarvestedPayload(url=url, operation=operation, data=document))
        except Exception as error:  # noqa: BLE001 - a bad response must not kill the crawl
            logger.debug("Harvester skipped a response: %s", error)

    # ---------------------------------------------------------------- access

    @property
    def payloads(self) -> list[HarvestedPayload]:
        return list(self._payloads)

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def error_statuses(self) -> list[tuple[int, str]]:
        return list(self._error_statuses)

    def refused(self, url_fragment: str = "") -> int | None:
        """Newest 4xx/5xx status seen for URLs containing ``url_fragment``."""
        for status, url in reversed(self._error_statuses):
            if not url_fragment or url_fragment in url:
                return status
        return None

    def select(
        self,
        *,
        include_operations: Sequence[str] | None = None,
        exclude_operations: Sequence[str] | None = None,
    ) -> list[HarvestedPayload]:
        """Filter buffered payloads by GraphQL operation name.

        This is how "related / recommended" content is kept out of a post's
        reply list: those arrive from a separate query whose name differs from
        the one that renders the thread itself.
        """
        selected = []
        for payload in self._payloads:
            if exclude_operations and payload.operation_matches(exclude_operations):
                continue
            if include_operations and not payload.operation_matches(include_operations):
                continue
            selected.append(payload)
        return selected

    def clear(self) -> None:
        self._payloads.clear()
        self._dropped = 0


async def _safe_headers(response: Response) -> dict[str, str]:
    try:
        return {key.lower(): value for key, value in (await response.all_headers()).items()}
    except Exception:  # pragma: no cover
        return {}


def _looks_like_json(content_type: str) -> bool:
    lowered = content_type.lower()
    return "json" in lowered or "javascript" in lowered or "text/plain" in lowered


def _operation_name(response: Response) -> str | None:
    request = response.request
    try:
        post_data = request.post_data
    except Exception:  # pragma: no cover - body may be unavailable
        post_data = None

    if post_data:
        if post_data.lstrip().startswith("{"):
            try:
                parsed = json.loads(post_data)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                for key in _OPERATION_KEYS:
                    value = parsed.get(key)
                    if isinstance(value, str) and value:
                        return value
        else:
            fields = parse_qs(post_data)
            for key in _OPERATION_KEYS:
                values = fields.get(key)
                if values and values[0]:
                    return values[0]

    query = parse_qs(urlparse(response.url).query)
    for key in _OPERATION_KEYS:
        values = query.get(key)
        if values and values[0]:
            return values[0]

    path = urlparse(response.url).path.rstrip("/")
    return path.rsplit("/", 1)[-1] or None


def loads_multi(body: str, *, max_documents: int = 50) -> list[Any]:
    """Parse a body that may hold one JSON document, or several concatenated."""
    text = body.lstrip()
    for prefix in _JSON_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix) :].lstrip()
            break
    if not text or text[0] not in "{[":
        return []

    try:
        return [json.loads(text)]
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    documents: list[Any] = []
    index = 0
    length = len(text)
    while index < length and len(documents) < max_documents:
        while index < length and text[index] in " \t\r\n,;":
            index += 1
        if index >= length or text[index] not in "{[":
            break
        try:
            document, index = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            break
        documents.append(document)
    return documents
