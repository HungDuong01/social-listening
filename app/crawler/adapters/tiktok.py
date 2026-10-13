"""TikTok adapter.

TikTok's web app talks to a plain REST-ish API (``/api/search/...``,
``/api/comment/list/``) whose payloads are far friendlier than Meta's GraphQL:
every item carries a ``stats`` block with exact counters, so the DOM is only
needed to discover which videos the search returned.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlencode

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from app.core.enums import MetricSource, Platform
from app.core.exceptions import PlatformBlockedError
from app.core.utils import dig, epoch_to_datetime, extract_hashtags, extract_mentions
from app.core.utils import jitter_sleep, normalize_text
from app.crawler.base import CrawlContext, CrawlOptions, PlatformAdapter, register_adapter
from app.crawler.harvester import NetworkHarvester
from app.crawler.miner import collect_nodes
from app.crawler.scroller import infinite_scroll
from app.domain.entities import AuthorRef, CrawlResult, Metrics, PostRef, RawComment, RawPost

VIDEO_LINK_SELECTOR = 'a[href*="/video/"]'
COMMENT_SELECTOR = '[data-e2e="comment-level-1"]'
# The tab label is localised and carries no stable attribute to target.
_COMMENT_TAB_LABELS = ("Bình luận", "Comments", "Comment")
_MAX_COMMENT_ROUNDS = 150
_COMMENT_IDLE_ROUNDS = 3
# How many quiet rounds to tolerate while the feed still reports more pages.
_IDLE_ROUNDS_WHEN_MORE = 10
_RENDER_SETTLE_MIN_MS = 250
_RENDER_SETTLE_MAX_MS = 650
# Rounds to wait for the very first comment page before giving up on a video.
_STARTUP_GRACE_ROUNDS = 8
_COMMENT_TAB_TIMEOUT_MS = 12_000
_MAX_REPLY_EXPANSIONS = 80
# "Xem 5 câu trả lời" / "View 5 replies" - the count varies, the wording does not.
_REPLY_EXPANDER_RE = re.compile(
    r"(xem|view)\s+\d+\s+(câu\s+trả\s+lời|repl)", re.IGNORECASE
)

_SCROLL_COMMENTS_JS = """
(selector) => {
  const rows = document.querySelectorAll(selector);
  if (!rows.length) return false;
  let node = rows[rows.length - 1].parentElement;
  while (node) {
    const style = getComputedStyle(node);
    const scrollable = /(auto|scroll)/.test(style.overflowY);
    if (scrollable && node.scrollHeight > node.clientHeight + 8) {
      const before = node.scrollTop;
      node.scrollTop = node.scrollHeight;
      node.dispatchEvent(new Event('scroll', { bubbles: true }));
      return node.scrollTop > before;
    }
    node = node.parentElement;
  }
  return false;
}
"""
_VIDEO_HREF_RE = re.compile(r"/@([A-Za-z0-9._]+)/video/(\d+)")

_DISMISS = (
    'button:has-text("Decline all")',
    'button:has-text("Allow all")',
    'div[role="dialog"] button[aria-label="Close"]',
    'button:has-text("Từ chối tất cả")',
)


@register_adapter
class TikTokAdapter(PlatformAdapter):
    platform = Platform.TIKTOK
    display_name = "TikTok"
    home_url = "https://www.tiktok.com"
    login_url = "https://www.tiktok.com/login"
    network_patterns = (r"/api/search/", r"/api/comment/", r"/api/post/", r"/api/item/")
    excluded_operations = ("recommend", "related")
    # Measured against the live site with a truthful UA and a persistent
    # profile: a signed-out search in a *headed* window returns results
    # normally (11-12 videos for a Vietnamese keyword). The same search run
    # headless is answered with "Da xay ra loi" and zero results.
    #
    # So TikTok does not want a login - it wants a real window. An earlier
    # revision set requires_login=True after seeing only the headless refusal,
    # which confused "turned away" with "needs an account".
    requires_login = False
    prefers_headed = True
    # One budget for every request, not just page loads: a single video now
    # costs one navigation plus ~65 comment/reply fetches. Measured, ~56
    # req/min is where TikTok starts answering 403, so this sits below it.
    rate_per_minute = 40
    # No free burst: every video page waits its turn from the very first one.
    rate_burst = 1
    session_cookie_names = ("sessionid",)
    cookie_urls = ("https://www.tiktok.com",)
    # TikTok ships __UNIVERSAL_DATA_FOR_REHYDRATION__ as an inline JSON script.
    inline_json_keywords = ("diggCount", "itemInfo", "webapp.video-detail")

    # ------------------------------------------------------------------ search

    def build_search_url(self, keyword: str, options: CrawlOptions) -> str:
        return f"{self.home_url}/search?{urlencode({'q': keyword.strip()})}"

    async def is_authenticated(self, page: Page) -> bool:
        """Usable, not signed in.

        TikTok serves anonymous browsing, so "authenticated" here means the
        site is willing to talk to us at all. Requiring a session cookie -
        which is what this used to do - made every anonymous crawl fail the
        gate even though anonymous crawling works.
        """
        if await self.has_live_session(page.context):
            return True
        try:
            await page.goto(self.home_url, wait_until="domcontentloaded")
        except PlaywrightError:
            return False
        return not await self._is_blocked(page)

    async def collect_post_refs(self, page: Page, ctx: CrawlContext) -> list[PostRef]:
        search_url = self.build_search_url(ctx.keyword, ctx.options)
        harvester = self.make_harvester()
        harvester.attach(page)
        try:
            await self.navigate(page, search_url, ctx)
            await self.dismiss_overlays(page, _DISMISS)

            target = ctx.options.max_posts

            # Same virtualised-feed pattern confirmed live on Threads (shared
            # infinite_scroll): a plain page.locator(...).count() per round
            # can plateau once the feed recycles off-screen links out of the
            # DOM, well before the feed itself runs dry, and a final
            # one-shot DOM query after scrolling stops loses whatever
            # already got recycled out. Fixed by accumulating every round's
            # DOM links into one running, order-preserving map.
            dom_links_seen: dict[str, tuple[str, str]] = {}

            async def count_items() -> int:
                for username, video_id in await self._dom_video_links(page):
                    dom_links_seen.setdefault(video_id, (username, video_id))
                return len(dom_links_seen)

            async def on_round(round_index: int, seen: int) -> None:
                ctx.raise_if_cancelled()
                if round_index % 3 == 0:
                    await ctx.report(f"scrolling search results ({seen} videos seen)")

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
            api_items = collect_nodes(search_documents, _is_item_node, key=_item_identity)
            dom_links = list(dom_links_seen.values())
        finally:
            harvester.detach()

        by_id = {_item_identity(item): item for item in api_items if _item_identity(item)}
        refs: list[PostRef] = []
        seen_ids: set[str] = set()

        for username, video_id in dom_links:
            item = by_id.get(video_id)
            refs.append(
                PostRef(
                    platform=self.platform,
                    external_id=video_id,
                    url=self._video_url(username, video_id),
                    author_username=username,
                    rank=len(refs) + 1,
                    preview_metrics=_metrics_from_item(item) if item else Metrics(),
                    raw=None,
                )
            )
            seen_ids.add(video_id)

        for video_id, item in by_id.items():
            if video_id in seen_ids:
                continue
            username = str(dig(item, "author", "uniqueId") or "").strip()
            if not username:
                continue
            refs.append(
                PostRef(
                    platform=self.platform,
                    external_id=video_id,
                    url=self._video_url(username, video_id),
                    author_username=username,
                    rank=len(refs) + 1,
                    preview_metrics=_metrics_from_item(item),
                    raw=None,
                )
            )

        if not refs and await self._search_unavailable(page):
            raise PlatformBlockedError(
                self.platform.value,
                "trả về trang lỗi thay vì kết quả tìm kiếm",
                remedy="TikTok từ chối trình duyệt headless - đặt SC_HEADLESS=false "
                "rồi chạy lại; đăng nhập không giải quyết được lỗi này",
            )
        return refs[: ctx.options.max_posts]

    # ------------------------------------------------------------------- video

    async def fetch_post(self, page: Page, ref: PostRef, ctx: CrawlContext) -> CrawlResult:
        harvester = self.make_harvester()
        harvester.attach(page)
        notes: list[str] = []
        try:
            # We genuinely reached this video from the search results, so send
            # the referer a browser would have sent on that click.
            await self.navigate(
                page,
                ref.url,
                ctx,
                referer=self.build_search_url(ctx.keyword, ctx.options),
            )
            await self.dismiss_overlays(page, _DISMISS)
            if await self._is_blocked(page):
                notes.append("TikTok served a verification wall for this video")
            if ctx.options.include_comments:
                await self._load_comments(page, ctx, harvester)

            documents: list[Any] = [payload.data for payload in harvester.select()]
            documents.extend(await self.inline_documents(page))
            items = collect_nodes(documents, _is_item_node, key=_item_identity)
            comment_nodes = collect_nodes(
                documents, _is_comment_node, key=lambda node: str(node.get("cid") or "")
            )
        finally:
            harvester.detach()

        item = next((node for node in items if _item_identity(node) == ref.external_id), None)
        if item is None:
            notes.append("video JSON not intercepted - only the reference data is stored")
            post = RawPost(
                platform=self.platform,
                external_id=ref.external_id,
                url=ref.url,
                author=AuthorRef(
                    platform=self.platform, username=ref.author_username or "unknown"
                ),
                metrics=ref.preview_metrics,
            )
        else:
            post = self._post_from_item(item, ref)

        comments: list[RawComment] = []
        truncated = False
        if ctx.options.include_comments:
            # A limit of 0 means "no limit"; slicing with [:0] would silently
            # discard every comment just collected.
            limit = ctx.options.max_comments_per_post or None
            truncated = limit is not None and len(comment_nodes) > limit
            selected = comment_nodes[:limit] if limit is not None else comment_nodes
            comments = [
                self._comment_from_node(node, ref.external_id, post.author.username)
                for node in selected
            ]

            # Say plainly *why* the count differs. "Fewer than expected" covers
            # three very different situations and conflating them makes a
            # complete crawl look like a broken one.
            expected = post.metrics.comments
            got = len(comments)
            if expected:
                if got >= expected:
                    notes.append(f"đã lấy đủ {got}/{expected} comment")
                elif truncated:
                    notes.append(f"dừng ở giới hạn {limit} comment (video có {expected})")
                elif got == 0:
                    # Claiming the feed was exhausted when nothing arrived
                    # hides a real failure behind a reassuring sentence.
                    notes.append(
                        f"khong lay duoc comment nao (video co {expected}) "
                        "- feed comment khong tra ve du lieu"
                    )
                elif not _feed_has_more(harvester):
                    # The feed said it had nothing left and every collapsed
                    # reply thread was opened, so this is all TikTok serves.
                    # The remainder is counted by the platform but withheld -
                    # deleted, filtered or hidden comments.
                    notes.append(
                        f"đã vét hết feed: {got}/{expected} comment. "
                        f"{expected - got} comment còn lại được TikTok đếm nhưng không phục vụ "
                        "(bị ẩn/lọc/xoá)"
                    )
                else:
                    notes.append(
                        f"chỉ lấy được {got}/{expected} comment - feed vẫn báo còn nữa"
                    )

        return CrawlResult(post=post, comments=comments, comments_truncated=truncated, notes=notes)

    # ----------------------------------------------------------------- helpers

    def _video_url(self, username: str, video_id: str) -> str:
        return f"{self.home_url}/@{username}/video/{video_id}"

    async def _dom_video_links(self, page: Page) -> list[tuple[str, str]]:
        try:
            hrefs: list[str] = await page.evaluate(
                "() => Array.from(document.querySelectorAll('a[href*=\"/video/\"]'))"
                ".map((anchor) => anchor.getAttribute('href') || '')"
            )
        except PlaywrightError:
            return []

        links: list[tuple[str, str]] = []
        seen: set[str] = set()
        for href in hrefs or []:
            match = _VIDEO_HREF_RE.search(href)
            if match and match.group(2) not in seen:
                seen.add(match.group(2))
                links.append((match.group(1), match.group(2)))
        return links

    async def _load_comments(
        self, page: Page, ctx: CrawlContext, harvester: NetworkHarvester
    ) -> int:
        """Open the comments tab, then page through every comment.

        A TikTok video page opens on the "You may like" tab, not on comments,
        so nothing comment-related is ever requested until that tab is
        clicked - which is why this used to return zero comments on videos
        that plainly had dozens. The tab carries no data-e2e or role
        attribute, so it is located by its label.
        """
        if not await self._open_comments_tab(page):
            self.logger.info("No comments tab on %s - the video may have them disabled", page.url)
            return 0

        # 0 means "every comment there is".
        target = ctx.options.max_comments_per_post
        seen = 0
        idle = 0
        count = 0
        started_grace = 0

        for _ in range(_MAX_COMMENT_ROUNDS):
            # Every scroll round triggers a comment XHR, so it has to draw from
            # the same budget as a page load. TikTok counts all requests alike;
            # throttling only navigations let this loop run at ~56 req/min
            # while the configured limit claimed to be 10.
            await ctx.throttle()
            count = await self.count_links(page, COMMENT_SELECTOR)

            if target and count >= target:
                break

            refused = harvester.refused("/api/comment")
            if refused:
                cooldown = ctx.penalise(45.0)
                self.logger.warning(
                    "TikTok refused the comment feed with HTTP %s - pausing %.0fs. "
                    "Collected %s comments before the refusal.",
                    refused,
                    cooldown,
                    count,
                )
                break

            # The feed itself says whether more pages exist. Deciding that from
            # the DOM row count alone is guesswork: a batch that is merely slow
            # looks exactly like the end of the list, which is how this used to
            # stop at ~88% of a long thread.
            # Before the first batch lands there is nothing to compare against:
            # count and seen are both 0 and no payload has set has_more yet, so
            # the idle test below would fire immediately and abandon the video
            # with zero comments. Wait for the feed to actually start.
            if count == 0 and not _feed_started(harvester):
                if started_grace < _STARTUP_GRACE_ROUNDS:
                    started_grace += 1
                    await self._scroll_comment_panel(page)
                    await jitter_sleep(_RENDER_SETTLE_MIN_MS, _RENDER_SETTLE_MAX_MS)
                    continue
                break

            has_more = _feed_has_more(harvester)

            if count == seen:
                idle += 1
                # Be patient while the platform still has pages to give.
                tolerance = _IDLE_ROUNDS_WHEN_MORE if has_more else _COMMENT_IDLE_ROUNDS
                if idle >= tolerance:
                    break
            else:
                idle = 0
                seen = count

            await self._scroll_comment_panel(page)
            # Just long enough for the batch to render - the rate limiter above
            # already sets the pace, so the old full scroll delay here meant
            # waiting twice for every round.
            await jitter_sleep(_RENDER_SETTLE_MIN_MS, _RENDER_SETTLE_MAX_MS)

        await self._expand_reply_threads(page, ctx)
        return await self.count_links(page, COMMENT_SELECTOR)

    async def _expand_reply_threads(self, page: Page, ctx: CrawlContext) -> int:
        """Open every collapsed "view N replies" thread.

        Replies are folded away behind a link and are never fetched until it is
        clicked, so a crawl that only scrolls the top-level list silently
        misses them - they are counted in the video's comment total all the
        same.
        """
        opened = 0
        for _ in range(_MAX_REPLY_EXPANSIONS):
            # Each expansion fetches a reply page - same budget as everything else.
            await ctx.throttle()
            link = page.get_by_text(_REPLY_EXPANDER_RE).first
            try:
                if not await link.count():
                    break
                await link.click(timeout=4_000)
            except PlaywrightError:
                break
            opened += 1
            await jitter_sleep(700, 1_400)

        if opened:
            self.logger.debug("Expanded %s collapsed reply threads", opened)
        return opened

    async def _scroll_comment_panel(self, page: Page) -> bool:
        """Drive the comment list's own scroll container to its bottom.

        The list lives in a nested ``overflow-y: scroll`` div. Scrolling the
        window - or calling ``scrollIntoView`` on a row - leaves that
        container's ``scrollTop`` at 0, so its infinite-scroll handler never
        fires and the crawl sits on the first page forever, even though the
        API reports ``has_more: 1``.
        """
        try:
            return bool(await page.evaluate(_SCROLL_COMMENTS_JS, COMMENT_SELECTOR))
        except PlaywrightError:  # pragma: no cover - page can navigate away
            return False

    async def _open_comments_tab(self, page: Page) -> bool:
        """Click the comments tab, waiting for it to render first.

        The right-hand panel is built a few seconds after the document is
        ready. Probing with a single ``count()`` therefore succeeds when run by
        hand and fails inside a crawl, which is exactly how this produced
        "0 comments" on videos that had dozens.
        """
        for label in _COMMENT_TAB_LABELS:
            tab = page.get_by_text(label, exact=True).first
            try:
                await tab.wait_for(state="visible", timeout=_COMMENT_TAB_TIMEOUT_MS)
            except PlaywrightError:
                continue
            try:
                await tab.click(timeout=5_000)
            except PlaywrightError:
                continue
            # The first batch arrives over the network, so give it a moment.
            try:
                await page.locator(COMMENT_SELECTOR).first.wait_for(
                    state="visible", timeout=_COMMENT_TAB_TIMEOUT_MS
                )
            except PlaywrightError:
                pass
            return True

        # Already on the comments tab? Then the rows are there without a click.
        return await self.count_links(page, COMMENT_SELECTOR) > 0

    async def _is_blocked(self, page: Page) -> bool:
        """A *visible* challenge, not merely a challenge element in the DOM.

        TikTok injects and removes captcha containers during normal page load:
        a run that returned 12 search results also had one present for a
        moment. Testing for presence alone reported every healthy page as
        blocked, so visibility is what counts.
        """
        try:
            locator = page.locator('div[id*="captcha"], .captcha_verify_container')
            for index in range(await locator.count()):
                if await locator.nth(index).is_visible():
                    return True
            return False
        except PlaywrightError:  # pragma: no cover
            return False

    async def _search_unavailable(self, page: Page) -> bool:
        """Tell "TikTok refused us" apart from "the keyword has no matches"."""
        if await self._is_blocked(page):
            return True
        try:
            text = (await page.inner_text("body"))[:4_000].lower()
        except PlaywrightError:  # pragma: no cover
            return False
        markers = (
            "đã xảy ra lỗi",
            "something went wrong",
            "vấn đề với máy chủ",
            "problem with the server",
            "log in to continue",
            "đăng nhập để tiếp tục",
        )
        return any(marker in text for marker in markers)

    def _post_from_item(self, item: dict[str, Any], ref: PostRef) -> RawPost:
        author_node = item.get("author") or {}
        username = str(author_node.get("uniqueId") or ref.author_username or "unknown")
        author = AuthorRef(
            platform=self.platform,
            username=username,
            external_id=str(author_node.get("id")) if author_node.get("id") else None,
            display_name=author_node.get("nickname") or None,
            avatar_url=author_node.get("avatarThumb") or None,
            is_verified=author_node.get("verified")
            if isinstance(author_node.get("verified"), bool)
            else None,
            follower_count=_as_int(dig(item, "authorStats", "followerCount")),
        )
        content = normalize_text(str(item.get("desc") or ""))
        video_id = _item_identity(item) or ref.external_id

        return RawPost(
            platform=self.platform,
            external_id=video_id,
            url=self._video_url(username, video_id),
            author=author,
            content=content,
            shortcode=video_id,
            posted_at=epoch_to_datetime(item.get("createTime")),
            metrics=_metrics_from_item(item),
            hashtags=extract_hashtags(content),
            mentions=extract_mentions(content),
            raw={"id": video_id, "createTime": item.get("createTime")},
        )

    def _comment_from_node(
        self, node: dict[str, Any], post_external_id: str, root_author: str
    ) -> RawComment:
        user = node.get("user") or {}
        username = str(user.get("unique_id") or user.get("uniqueId") or "").strip()
        author = AuthorRef(
            platform=self.platform,
            username=username or "unknown",
            external_id=str(user.get("uid")) if user.get("uid") else None,
            display_name=user.get("nickname") or None,
        )
        parent = node.get("reply_id")
        # TikTok marks a top-level comment with reply_id "0", not null.
        parent_id = str(parent) if parent and str(parent) not in {"0", ""} else None

        return RawComment(
            platform=self.platform,
            external_id=str(node.get("cid")),
            post_external_id=post_external_id,
            author=author,
            content=normalize_text(str(node.get("text") or "")),
            parent_external_id=parent_id,
            posted_at=epoch_to_datetime(node.get("create_time")),
            like_count=_as_int(node.get("digg_count")),
            reply_count=_as_int(node.get("reply_comment_total")),
            depth=2 if parent_id else 1,
            is_author_reply=bool(root_author) and username.lower() == root_author.lower(),
        )


def _feed_started(harvester: NetworkHarvester) -> bool:
    """Whether any comment page has come back yet."""
    return any("/api/comment" in payload.url for payload in harvester.payloads)


def _feed_has_more(harvester: NetworkHarvester) -> bool:
    """Whether the newest comment page said more are available.

    TikTok answers every ``/api/comment/list/`` call with ``has_more`` and a
    cursor. That flag is the only reliable "keep going" signal - the rendered
    row count cannot tell a slow page from the end of the thread.
    """
    for payload in reversed(harvester.payloads):
        if "/api/comment" not in payload.url:
            continue
        if isinstance(payload.data, dict) and "has_more" in payload.data:
            return bool(payload.data.get("has_more"))
    return False


def _is_item_node(node: dict[str, Any]) -> bool:
    if not isinstance(node, dict):
        return False
    if node.get("id") in (None, "") or not isinstance(node.get("author"), dict):
        return False
    return isinstance(node.get("stats"), dict) or "desc" in node


def _item_identity(node: dict[str, Any]) -> str | None:
    value = node.get("id")
    return str(value) if value not in (None, "") else None


def _is_comment_node(node: dict[str, Any]) -> bool:
    if not isinstance(node, dict):
        return False
    if not node.get("cid") or not isinstance(node.get("text"), str):
        return False
    return isinstance(node.get("user"), dict)


def _metrics_from_item(node: dict[str, Any] | None) -> Metrics:
    if not node:
        return Metrics()
    stats = node.get("stats") if isinstance(node.get("stats"), dict) else {}
    return Metrics(
        likes=_as_int(stats.get("diggCount")),
        comments=_as_int(stats.get("commentCount")),
        shares=_as_int(stats.get("shareCount")),
        views=_as_int(stats.get("playCount")),
        saves=_as_int(stats.get("collectCount")),
        source=MetricSource.API,
        approximate=False,
    )


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
