"""Read the JSON a server-rendered page embeds in its own markup.

This is the third extraction source, and it matters more than it looks.
Measured against a real Threads post page while signed out: the SPA issues **no**
GraphQL calls at all - the server renders everything up front - so network
interception captures nothing. The same page still ships ~1.3 MB of inline
``<script type="application/json">`` payloads carrying exact ``like_count``
values. Without this module a signed-out crawl would silently fall back to the
rounded DOM text ("1,9K") and lose precision it never actually needed to lose.

Priority order used by the adapters:

1. intercepted XHR/GraphQL JSON  - exact (signed-in SPA)
2. inline SSR JSON               - exact (signed-out server render)
3. rendered DOM text             - rounded, last resort
"""

from __future__ import annotations

from typing import Any, Sequence

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from app.core.logging import get_logger
from app.crawler.harvester import loads_multi

logger = get_logger(__name__)

_INLINE_JSON_JS = """
(options) => {
  const blobs = [];
  const nodes = document.querySelectorAll('script[type="application/json"]');
  for (const node of nodes) {
    const text = node.textContent || '';
    if (!text || text.length > options.maxBytes) continue;
    if (options.keywords.length && !options.keywords.some((k) => text.includes(k))) continue;
    blobs.push(text);
    if (blobs.length >= options.maxBlobs) break;
  }
  return blobs;
}
"""


async def extract_inline_json(
    page: Page,
    *,
    keywords: Sequence[str] = (),
    max_bytes: int = 3_000_000,
    max_blobs: int = 40,
) -> list[Any]:
    """Parse the page's embedded JSON blobs.

    ``keywords`` pre-filters in the browser so only blobs that can plausibly
    hold what the caller wants cross the bridge - a page carries dozens of
    unrelated config payloads.
    """
    try:
        blobs: list[str] = await page.evaluate(
            _INLINE_JSON_JS,
            {"keywords": list(keywords), "maxBytes": max_bytes, "maxBlobs": max_blobs},
        )
    except PlaywrightError as error:
        logger.debug("Inline JSON extraction failed: %s", error)
        return []

    documents: list[Any] = []
    for blob in blobs or []:
        documents.extend(loads_multi(blob))
    if documents:
        logger.debug("Recovered %s inline JSON document(s)", len(documents))
    return documents
