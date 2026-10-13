"""Crawler package.

Importing ``app.crawler`` also imports ``app.crawler.adapters`` so that every
adapter is registered before anything asks the registry for one.
"""

from app.crawler.base import (
    CrawlContext,
    CrawlOptions,
    PlatformAdapter,
    adapter_descriptors,
    get_adapter,
    is_throttle_error,
    register_adapter,
    registered_platforms,
)
from app.crawler.browser import BrowserManager
from app.crawler.ratelimit import RateLimiter

from app.crawler import adapters as _adapters  # noqa: F401  (registration side effect)

__all__ = [
    "BrowserManager",
    "CrawlContext",
    "CrawlOptions",
    "PlatformAdapter",
    "RateLimiter",
    "adapter_descriptors",
    "get_adapter",
    "is_throttle_error",
    "register_adapter",
    "registered_platforms",
]
