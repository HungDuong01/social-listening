"""Parsing helpers shared by the two Meta properties (Threads, Instagram).

Both serve the same media-node shape from ``/api/v1/`` and ``/graphql/query``,
so identity, author, media and text extraction are written once here. Metric
field names differ per product and stay in the individual adapters.
"""

from __future__ import annotations

from typing import Any

from app.core.enums import Platform
from app.core.utils import dig, normalize_text
from app.domain.entities import AuthorRef, MediaItem


_NODE_MARKERS = ("caption", "text_post_app_info", "code", "like_count", "taken_at", "media_type")


def is_media_node(node: dict[str, Any]) -> bool:
    """Heuristic test for "this dict describes a post".

    Deliberately loose: Meta wraps posts in differently named containers across
    endpoints, and a strict path check breaks the moment a wrapper is renamed.
    """
    if not isinstance(node, dict):
        return False
    if node.get("pk") is None and node.get("id") is None:
        return False

    user = node.get("user")
    if not isinstance(user, dict) or not user.get("username"):
        return False

    return sum(1 for marker in _NODE_MARKERS if marker in node) >= 2


def node_identity(node: dict[str, Any]) -> str | None:
    """Shortcode when available - it is what appears in the post URL."""
    code = node.get("code")
    if isinstance(code, str) and code:
        return code
    for key in ("pk", "id"):
        value = node.get(key)
        if value not in (None, ""):
            return str(value).split("_")[0]
    return None


def build_author(node: dict[str, Any], platform: Platform) -> AuthorRef:
    user = node.get("user") or {}
    external_id = user.get("pk") or user.get("id")
    return AuthorRef(
        platform=platform,
        username=str(user.get("username") or "").strip(),
        external_id=str(external_id) if external_id not in (None, "") else None,
        display_name=user.get("full_name") or None,
        avatar_url=user.get("profile_pic_url") or None,
        is_verified=user.get("is_verified") if isinstance(user.get("is_verified"), bool) else None,
        follower_count=_as_int(user.get("follower_count")),
    )


def extract_text(node: dict[str, Any]) -> str:
    for path in (("caption", "text"), ("text",), ("title",)):
        value = dig(node, *path)
        if isinstance(value, str) and value.strip():
            return normalize_text(value)
    return ""


def build_media(node: dict[str, Any]) -> list[MediaItem]:
    items: list[MediaItem] = []

    for candidate in _carousel_nodes(node):
        image = dig(candidate, "image_versions2", "candidates", 0) or {}
        videos = candidate.get("video_versions") or []
        if videos:
            video = videos[0] if isinstance(videos, list) and videos else {}
            items.append(
                MediaItem(
                    kind="video",
                    url=str(video.get("url") or ""),
                    thumbnail_url=image.get("url"),
                    width=_as_int(video.get("width")),
                    height=_as_int(video.get("height")),
                )
            )
        elif image.get("url"):
            items.append(
                MediaItem(
                    kind="image",
                    url=str(image["url"]),
                    thumbnail_url=None,
                    width=_as_int(image.get("width")),
                    height=_as_int(image.get("height")),
                )
            )

    return [item for item in items if item.url]


def _carousel_nodes(node: dict[str, Any]) -> list[dict[str, Any]]:
    carousel = node.get("carousel_media")
    if isinstance(carousel, list) and carousel:
        return [item for item in carousel if isinstance(item, dict)]
    return [node]


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


as_int = _as_int
