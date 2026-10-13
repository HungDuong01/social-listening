"""Typed errors so callers can react to *why* something failed."""

from __future__ import annotations


class CrawlerError(Exception):
    """Base class for every error raised by this application."""


class ConfigurationError(CrawlerError):
    pass


class BrowserNotReadyError(CrawlerError):
    pass


class AuthenticationRequiredError(CrawlerError):
    """The platform served a login wall instead of the requested content."""

    def __init__(self, platform: str, message: str | None = None) -> None:
        self.platform = platform
        super().__init__(message or f"{platform}: a logged-in session is required")


class PlatformBlockedError(CrawlerError):
    """The platform refused this session for a reason logging in cannot fix.

    Kept separate from :class:`AuthenticationRequiredError` on purpose: telling
    somebody to sign in when the real cause is, say, a headless browser being
    turned away sends them to do work that cannot possibly help.
    """

    def __init__(self, platform: str, message: str, *, remedy: str | None = None) -> None:
        self.platform = platform
        self.remedy = remedy
        super().__init__(f"{platform}: {message}" + (f" ({remedy})" if remedy else ""))


class NavigationError(CrawlerError):
    pass


class ExtractionError(CrawlerError):
    """The page loaded but nothing usable could be pulled out of it."""


class JobCancelledError(CrawlerError):
    pass


class UnknownPlatformError(CrawlerError):
    pass
