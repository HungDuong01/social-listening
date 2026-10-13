"""Instagram adapter.

Shares Meta's media-node shape with Threads, so identity/author/media parsing
is reused. What differs: the search entry point, the metric field names, and
the fact that comments arrive from a dedicated ``/comments/`` endpoint whose
nodes look nothing like a post.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urlencode

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from app.core.enums import MetricSource, Platform
from app.core.exceptions import AuthenticationRequiredError
from app.core.utils import dig, epoch_to_datetime, extract_hashtags, extract_mentions
from app.core.utils import jitter_sleep, normalize_text
from app.crawler.adapters.meta_common import (
    as_int,
    build_author,
    build_media,
    extract_text,
    is_media_node,
    node_identity,
)
from app.crawler.base import CrawlContext, CrawlOptions, PlatformAdapter, register_adapter
from app.crawler.miner import collect_nodes
from app.crawler.scroller import ScrollPolicy, infinite_scroll
from app.domain.entities import AuthorRef, CrawlResult, Metrics, PostRef, RawComment, RawPost

POST_LINK_SELECTOR = 'a[href*="/p/"], a[href*="/reel/"]'
_POST_HREF_RE = re.compile(r"/(?:p|reel)/([A-Za-z0-9_-]+)")

_MAX_LOAD_MORE_CLICKS = 60
_MAX_REPLY_EXPANSIONS = 80
# "Xem N cau tra loi" / "Xem N phan hoi" - confirmed on Facebook's Vietnamese
# UI, which shares Instagram's comment component design; English kept as a
# fallback for accounts whose UI language differs.
_REPLY_EXPANDER_RE = re.compile(
    r"(xem|view)\s+\d+\s+(câu\s*trả\s*lời|phản\s*hồi|repl)",
    re.IGNORECASE,
)

_COOKIE_DISMISS = (
    'button:has-text("Allow all cookies")',
    'button:has-text("Decline optional cookies")',
    'button:has-text("Cho phép tất cả cookie")',
)


@register_adapter
class InstagramAdapter(PlatformAdapter):
    platform = Platform.INSTAGRAM
    display_name = "Instagram"
    home_url = "https://www.instagram.com"
    login_url = "https://www.instagram.com/accounts/login/"
    network_patterns = (r"/api/v1/", r"/graphql/query", r"/api/graphql")
    excluded_operations = ("related", "suggested", "explore_grid", "recommend")
    requires_login = True
    session_cookie_names = ("sessionid",)
    cookie_urls = ("https://www.instagram.com",)
    inline_json_keywords = ("like_count", "comment_count", "edge_media_preview_like")

    # ------------------------------------------------------------------ search

    def build_search_url(self, keyword: str, options: CrawlOptions) -> str:
        stripped = keyword.strip()
        if stripped.startswith("#"):
            tag = re.sub(r"[^\wÀ-ỹ]", "", stripped[1:])
            if tag:
                return f"{self.home_url}/explore/tags/{quote(tag)}/"
        return f"{self.home_url}/explore/search/keyword/?{urlencode({'q': stripped})}"

    async def is_authenticated(self, page: Page) -> bool:
        """Cheap cookie check first, then the authoritative page-load check.

        Cookie presence alone is not proof of a working session - measured
        directly on this platform: the previous "cookie, else check the home
        page for a login wall" approach reported Instagram as authenticated
        with zero live cookies, because the logged-out home page did not
        happen to look walled. ``confirm_session`` is the same authoritative
        check the login flow itself trusts before saving a session.
        """
        if not await self.has_live_session(page.context):
            return False
        confirmed, _detail = await self.confirm_session(page.context)
        return confirmed

    async def collect_post_refs(self, page: Page, ctx: CrawlContext) -> list[PostRef]:
        search_url = self.build_search_url(ctx.keyword, ctx.options)
        harvester = self.make_harvester()
        harvester.attach(page)
        try:
            await self.navigate(page, search_url, ctx)
            await self.dismiss_overlays(page, _COOKIE_DISMISS)
            if await self._shows_login_wall(page):
                raise AuthenticationRequiredError(
                    self.platform.value, "Instagram search requires a logged-in session"
                )

            target = ctx.options.max_posts

            # Confirmed live on Threads (same virtualised-feed pattern, same
            # shared infinite_scroll): a plain page.locator(...).count() per
            # round plateaus once the feed starts recycling off-screen post
            # links out of the DOM, well before the feed itself runs dry -
            # the scroll then gives up thinking nothing new is loading. A
            # one-shot DOM query after scrolling stops has the same problem
            # in reverse: whatever got recycled out by then is gone. Both
            # are fixed by accumulating every round's DOM codes into one
            # running, order-preserving map instead.
            dom_codes_seen: dict[str, str] = {}

            async def count_items() -> int:
                for code in await self._dom_post_codes(page):
                    dom_codes_seen.setdefault(code, code)
                return len(dom_codes_seen)

            async def on_round(round_index: int, seen: int) -> None:
                ctx.raise_if_cancelled()
                if round_index % 3 == 0:
                    await ctx.report(f"scrolling search results ({seen} links seen)")

            await infinite_scroll(
                page,
                policy=ctx.scroll_policy(),
                count_items=count_items,
                should_stop=lambda seen: seen >= target,
                on_round=on_round,
            )
            await count_items()  # capture whatever the last scroll round rendered

            search_documents: list[Any] = [payload.data for payload in harvester.select()]
            search_documents.extend(await self.inline_documents(page))
            api_nodes = collect_nodes(search_documents, is_media_node, key=node_identity)
            dom_codes = list(dom_codes_seen.values())
        finally:
            harvester.detach()

        by_code = {node_identity(node): node for node in api_nodes if node_identity(node)}
        refs: list[PostRef] = []
        seen: set[str] = set()

        for code in dom_codes:
            node = by_code.get(code)
            refs.append(self._make_ref(code, node, rank=len(refs) + 1))
            seen.add(code)
        for code, node in by_code.items():
            if code not in seen:
                refs.append(self._make_ref(code, node, rank=len(refs) + 1))

        return refs[: ctx.options.max_posts]

    # -------------------------------------------------------------------- post

    async def fetch_post(self, page: Page, ref: PostRef, ctx: CrawlContext) -> CrawlResult:
        harvester = self.make_harvester()
        harvester.attach(page)
        notes: list[str] = []
        try:
            await self.navigate(page, ref.url, ctx)
            if await self._shows_login_wall(page):
                raise AuthenticationRequiredError(
                    self.platform.value, "Instagram post page requires a logged-in session"
                )
            if ctx.options.include_comments:
                await self._load_comments(page, ctx)

            payloads = harvester.select(exclude_operations=self.excluded_operations)
            documents: list[Any] = [payload.data for payload in payloads]
            documents.extend(await self.inline_documents(page))
            media_nodes = collect_nodes(documents, is_media_node, key=node_identity)
            comment_nodes = collect_nodes(
                documents, _is_comment_node, key=lambda node: str(node.get("pk") or node.get("id"))
            )
        finally:
            harvester.detach()

        root_node = next(
            (node for node in media_nodes if node_identity(node) == ref.external_id), None
        )
        if root_node is None:
            root_node = media_nodes[0] if media_nodes else None
            if root_node is not None:
                notes.append("matched the post by position - shortcode was absent from the JSON")

        if root_node is None:
            notes.append("post JSON not intercepted")
            post = RawPost(
                platform=self.platform,
                external_id=ref.external_id,
                url=ref.url,
                author=AuthorRef(
                    platform=self.platform, username=ref.author_username or "unknown"
                ),
                shortcode=ref.external_id,
            )
        else:
            post = self._post_from_node(root_node, ref)

        comments: list[RawComment] = []
        truncated = False
        if ctx.options.include_comments:
            # 0 means no limit; [:0] would silently drop everything.
            limit = ctx.options.max_comments_per_post or None
            truncated = limit is not None and len(comment_nodes) > limit
            selected = comment_nodes[:limit] if limit is not None else comment_nodes
            comments = [
                self._comment_from_node(node, ref.external_id, post.author.username)
                for node in selected
            ]

        return CrawlResult(post=post, comments=comments, comments_truncated=truncated, notes=notes)

    # ----------------------------------------------------------------- helpers

    def _make_ref(self, code: str, node: dict[str, Any] | None, *, rank: int) -> PostRef:
        return PostRef(
            platform=self.platform,
            external_id=code,
            url=f"{self.home_url}/p/{code}/",
            author_username=str(dig(node or {}, "user", "username") or "") or None,
            rank=rank,
            preview_metrics=_metrics_from_node(node) if node else Metrics(),
            raw=node,
        )

    async def _load_comments(self, page: Page, ctx: CrawlContext) -> None:
        # A single click was measured, on Facebook's equivalent control, to
        # load at most one extra batch before the button needs clicking
        # again - the same real bug is assumed here rather than re-guessed,
        # since both platforms present a "load more" control that reveals a
        # fixed page per click. Looped, throttled, and bounded rather than
        # fired once.
        for _ in range(_MAX_LOAD_MORE_CLICKS):
            ctx.raise_if_cancelled()
            clicked = False
            for selector in (
                'button:has-text("Load more comments")',
                'button:has-text("View all comments")',
                'button:has-text("Xem tất cả")',
                'svg[aria-label="Load more comments"]',
            ):
                try:
                    locator = page.locator(selector).first
                    if await locator.count():
                        await locator.click(timeout=3_000)
                        clicked = True
                        break
                except PlaywrightError:
                    continue
            if not clicked:
                break
            await ctx.throttle()
            await jitter_sleep(self.settings.scroll_delay_min_ms, self.settings.scroll_delay_max_ms)

        await infinite_scroll(
            page,
            policy=ScrollPolicy(
                max_scrolls=self.settings.max_scrolls,
                idle_rounds=4,
                min_delay_ms=self.settings.scroll_delay_min_ms,
                max_delay_ms=self.settings.scroll_delay_max_ms,
            ),
            count_items=lambda: self.count_links(page, "ul li"),
        )
        ctx.raise_if_cancelled()

        await self._expand_reply_threads(page, ctx)

    async def _expand_reply_threads(self, page: Page, ctx: CrawlContext) -> int:
        """Open every collapsed "View N replies" / "Xem N phan hoi" thread.

        Not independently load-tested against a live Instagram session in
        this pass (no valid session was available at the time), but the same
        collapsed-reply pattern was directly confirmed on both TikTok and
        Facebook, and Instagram's comment UI is built on the same design
        language as Facebook's (both are Meta products sharing components).
        Applying the same fix here is a reasoned extension of a proven
        pattern, not a guess made from nothing - it should be re-verified
        against a live session before being trusted as fully confirmed.
        """
        opened = 0
        for _ in range(_MAX_REPLY_EXPANSIONS):
            ctx.raise_if_cancelled()
            link = page.get_by_text(_REPLY_EXPANDER_RE).first
            try:
                if not await link.count():
                    break
                await link.click(timeout=4_000)
            except PlaywrightError:
                break
            opened += 1
            await ctx.throttle()
            await jitter_sleep(700, 1_400)

        if opened:
            self.logger.debug("Expanded %s collapsed reply thread(s)", opened)
        return opened

    async def _dom_post_codes(self, page: Page) -> list[str]:
        try:
            hrefs: list[str] = await page.evaluate(
                "() => Array.from(document.querySelectorAll('a[href*=\"/p/\"], a[href*=\"/reel/\"]'))"
                ".map((anchor) => anchor.getAttribute('href') || '')"
            )
        except PlaywrightError:
            return []

        codes: list[str] = []
        seen: set[str] = set()
        for href in hrefs or []:
            match = _POST_HREF_RE.search(href)
            if match and match.group(1) not in seen:
                seen.add(match.group(1))
                codes.append(match.group(1))
        return codes

    async def _shows_login_wall(self, page: Page) -> bool:
        if "/accounts/login" in page.url:
            return True
        try:
            return await page.locator('input[name="password"]').count() > 0
        except PlaywrightError:  # pragma: no cover
            return False

    def _post_from_node(self, node: dict[str, Any], ref: PostRef) -> RawPost:
        author = build_author(node, self.platform)
        content = extract_text(node)
        code = node.get("code") if isinstance(node.get("code"), str) else ref.external_id
        return RawPost(
            platform=self.platform,
            external_id=code or ref.external_id,
            url=f"{self.home_url}/p/{code}/" if code else ref.url,
            author=author,
            content=content,
            shortcode=code,
            posted_at=epoch_to_datetime(node.get("taken_at")),
            metrics=_metrics_from_node(node),
            media=build_media(node),
            hashtags=extract_hashtags(content),
            mentions=extract_mentions(content),
            raw={"pk": node.get("pk"), "code": code, "media_type": node.get("media_type")},
        )

    def _comment_from_node(
        self, node: dict[str, Any], post_external_id: str, root_author: str
    ) -> RawComment:
        author = build_author(node, self.platform)
        parent = node.get("parent_comment_id") or node.get("parent_id")
        return RawComment(
            platform=self.platform,
            external_id=str(node.get("pk") or node.get("id")),
            post_external_id=post_external_id,
            author=author,
            content=normalize_text(str(node.get("text") or "")),
            parent_external_id=str(parent) if parent else None,
            posted_at=epoch_to_datetime(node.get("created_at") or node.get("created_at_utc")),
            like_count=as_int(node.get("comment_like_count") or node.get("like_count")),
            reply_count=as_int(node.get("child_comment_count")),
            depth=2 if parent else 1,
            is_author_reply=bool(root_author)
            and author.username.lower() == root_author.lower(),
            raw=None,
        )


def _is_comment_node(node: dict[str, Any]) -> bool:
    if not isinstance(node, dict):
        return False
    if not isinstance(node.get("text"), str) or not node.get("text").strip():
        return False
    if node.get("pk") is None and node.get("id") is None:
        return False
    user = node.get("user")
    if not isinstance(user, dict) or not user.get("username"):
        return False
    # A post carries a shortcode/caption; a comment never does.
    return "code" not in node and "caption" not in node


def _metrics_from_node(node: dict[str, Any]) -> Metrics:
    views = (
        node.get("play_count")
        or node.get("video_view_count")
        or node.get("view_count")
        or dig(node, "igtv_views")
    )
    return Metrics(
        likes=as_int(node.get("like_count")),
        comments=as_int(node.get("comment_count")),
        views=as_int(views),
        saves=as_int(node.get("saved_collection_count")),
        source=MetricSource.API,
        approximate=False,
    )
