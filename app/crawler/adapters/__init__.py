"""Importing this package registers every adapter with the crawler registry."""

from app.crawler.adapters.facebook import FacebookAdapter
from app.crawler.adapters.instagram import InstagramAdapter
from app.crawler.adapters.threads import ThreadsAdapter
from app.crawler.adapters.tiktok import TikTokAdapter

__all__ = ["FacebookAdapter", "InstagramAdapter", "ThreadsAdapter", "TikTokAdapter"]
