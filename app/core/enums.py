"""Domain enumerations shared across every layer."""

from __future__ import annotations

from enum import Enum


class Platform(str, Enum):
    THREADS = "threads"
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"
    TIKTOK = "tiktok"

    @property
    def label(self) -> str:
        return {
            Platform.THREADS: "Threads",
            Platform.INSTAGRAM: "Instagram",
            Platform.FACEBOOK: "Facebook",
            Platform.TIKTOK: "TikTok",
        }[self]


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {
            JobStatus.COMPLETED,
            JobStatus.PARTIAL,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }


class TargetStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NEEDS_AUTH = "needs_auth"
    BLOCKED = "blocked"


class MetricSource(str, Enum):
    """Where a metric value came from.

    API values are exact integers taken from the platform's own JSON responses.
    DOM values are parsed from rendered text and are therefore rounded
    ("1.9K" -> 1900) - the distinction matters when comparing numbers later.
    """

    API = "api"
    DOM = "dom"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class SearchFilter(str, Enum):
    TOP = "top"
    RECENT = "recent"


class SessionStatus(str, Enum):
    MISSING = "missing"
    SAVED = "saved"
    LOGGING_IN = "logging_in"
    VALID = "valid"
    INVALID = "invalid"
    ERROR = "error"
