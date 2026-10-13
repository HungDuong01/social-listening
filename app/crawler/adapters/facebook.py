"""Facebook adapter.

Facebook is the least stable of the four: class names are randomised per build
and the GraphQL payloads nest a story under several rotating wrapper keys.
That is precisely why extraction here is predicate-driven rather than
path-driven - the miner looks for *shapes* ("a dict with a post_id and a
feedback block"), which survives the wrapper churn that breaks fixed paths.

Expect this adapter to need field tuning more often than the others.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlencode, urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from app.core.enums import MetricSource, Platform
from app.core.exceptions import AuthenticationRequiredError
from app.core.utils import dig, epoch_to_datetime, extract_hashtags, extract_mentions
from app.core.utils import jitter_sleep, normalize_text
from app.crawler.base import CrawlContext, CrawlOptions, PlatformAdapter, register_adapter
from app.crawler.miner import collect_nodes
from app.crawler.scroller import ScrollPolicy, infinite_scroll
from app.domain.entities import AuthorRef, CrawlResult, Metrics, PostRef, RawComment, RawPost

POST_LINK_SELECTOR = (
    'a[href*="/posts/"], a[href*="story_fbid="], a[href*="/permalink/"], a[href*="/videos/"]'
)
_POST_ID_PATTERNS = (
    re.compile(r"/posts/(?:pfbid)?([A-Za-z0-9]+)"),
    re.compile(r"story_fbid=(?:pfbid)?([A-Za-z0-9]+)"),
    re.compile(r"/permalink/(\d+)"),
    re.compile(r"/videos/(\d+)"),
)

_MAX_LOAD_MORE_CLICKS = 60
_MAX_REPLY_EXPANSIONS = 80
# "Xem N cau tra loi" / "Xem N phan hoi" - Facebook Vietnamese uses both
# words for "reply" depending on context; the English form is kept as a
# fallback for accounts whose UI language is not Vietnamese.
_REPLY_EXPANDER_RE = re.compile(
    r"(xem|view)\s+\d+\s+(câu\s*trả\s*lời|phản\s*hồi|repl)",
    re.IGNORECASE,
)

_DISMISS = (
    'button:has-text("Allow all cookies")',
    'button:has-text("Decline optional cookies")',
    'button:has-text("Cho phép tất cả cookie")',
    'div[role="dialog"] div[aria-label="Close"]',
)


@register_adapter
class FacebookAdapter(PlatformAdapter):
    platform = Platform.FACEBOOK
    display_name = "Facebook"
    home_url = "https://www.facebook.com"
    login_url = "https://www.facebook.com/login"
    network_patterns = (r"/api/graphql", r"/graphql/")
    excluded_operations = ("suggested", "recommend", "related", "sponsored")
    requires_login = True
    # c_user alone is NOT a session: Facebook sets the account id early, while
    # a login challenge is still running. "xs" is the actual session token.
    session_cookie_names = ("c_user", "xs")
    # Present while the account is held in a checkpoint / verification flow.
    blocking_cookie_names = ("checkpoint",)
    cookie_urls = ("https://www.facebook.com",)
    inline_json_keywords = ("reaction_count", "feedback", "post_id")

    # ------------------------------------------------------------------ search

    def build_search_url(self, keyword: str, options: CrawlOptions) -> str:
        return f"{self.home_url}/search/posts/?{urlencode({'q': keyword.strip()})}"

    async def is_authenticated(self, page: Page) -> bool:
        """Cheap cookie check first, then the authoritative page-load check.

        Cookie presence alone is not proof of a working session - a session
        mid-checkpoint carries a live-looking cookie while the platform still
        refuses every page. ``confirm_session`` is the same authoritative
        check the login flow itself trusts before saving a session, so this
        stays consistent with what "logged in" means everywhere else in the
        app (see the Instagram false-positive this replaced).
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
            await self.dismiss_overlays(page, _DISMISS)
            if await self._shows_login_wall(page):
                raise AuthenticationRequiredError(
                    self.platform.value, "Facebook search requires a logged-in session"
                )

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
                for url, identity in await self._dom_post_links(page):
                    dom_links_seen.setdefault(identity, (url, identity))
                return len(dom_links_seen)

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
            stories = collect_nodes(search_documents, _is_story_node, key=_story_identity)
            dom_links = list(dom_links_seen.values())
        finally:
            harvester.detach()

        refs: list[PostRef] = []
        seen: set[str] = set()

        for story in stories:
            identity = _story_identity(story)
            if not identity or identity in seen:
                continue
            seen.add(identity)
            refs.append(
                PostRef(
                    platform=self.platform,
                    external_id=identity,
                    url=_story_url(story) or f"{self.home_url}/{identity}",
                    author_username=_author_username(story),
                    rank=len(refs) + 1,
                    preview_metrics=_metrics_from_story(story),
                    raw=None,
                )
            )

        for url, identity in dom_links:
            if identity in seen:
                continue
            seen.add(identity)
            refs.append(
                PostRef(
                    platform=self.platform,
                    external_id=identity,
                    url=url,
                    rank=len(refs) + 1,
                )
            )

        return refs[: ctx.options.max_posts]

    # -------------------------------------------------------------------- post

    async def fetch_post(self, page: Page, ref: PostRef, ctx: CrawlContext) -> CrawlResult:
        harvester = self.make_harvester()
        harvester.attach(page)
        notes: list[str] = []
        try:
            await self.navigate(page, ref.url, ctx)
            await self.dismiss_overlays(page, _DISMISS)
            if await self._shows_login_wall(page):
                raise AuthenticationRequiredError(
                    self.platform.value, "Facebook post page requires a logged-in session"
                )
            if ctx.options.include_comments:
                await self._load_comments(page, ctx)

            documents: list[Any] = [payload.data for payload in harvester.select()]
            documents.extend(await self.inline_documents(page))
            stories = collect_nodes(documents, _is_story_node, key=_story_identity)
            comment_nodes = collect_nodes(
                documents, _is_comment_node, key=lambda node: str(node.get("id") or "")
            )
        finally:
            harvester.detach()

        story = next(
            (node for node in stories if _story_identity(node) == ref.external_id), None
        )
        if story is None and stories:
            story = stories[0]
            notes.append("matched the story by position - post id was absent from the JSON")

        if story is None:
            notes.append("story JSON not intercepted")
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
            post = self._post_from_story(story, ref)

        comments: list[RawComment] = []
        truncated = False
        if ctx.options.include_comments:
            # 0 means no limit; [:0] would silently drop everything.
            limit = ctx.options.max_comments_per_post or None
            truncated = limit is not None and len(comment_nodes) > limit
            selected = comment_nodes[:limit] if limit is not None else comment_nodes
            comments = [
                self._comment_from_node(node, post.external_id, post.author.username)
                for node in selected
            ]

        return CrawlResult(post=post, comments=comments, comments_truncated=truncated, notes=notes)

    # ----------------------------------------------------------------- helpers

    async def _load_comments(self, page: Page, ctx: CrawlContext) -> None:
        # "Most relevant" only needs one click (it just changes sort order,
        # it does not reveal more comments) - everything that actually loads
        # more content is looped below instead of clicked once. A single
        # click was measured to be provably insufficient for TikTok's
        # equivalent control; nothing here justifies assuming Facebook's
        # button behaves differently.
        try:
            locator = page.locator('div[role="button"]:has-text("Most relevant")').first
            if await locator.count():
                await locator.click(timeout=3_000)
        except PlaywrightError:
            pass

        for _ in range(_MAX_LOAD_MORE_CLICKS):
            ctx.raise_if_cancelled()
            clicked = False
            for selector in (
                'div[role="button"]:has-text("View more comments")',
                'div[role="button"]:has-text("Xem thêm bình luận")',
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
            count_items=lambda: self.count_links(page, 'div[role="article"]'),
        )
        ctx.raise_if_cancelled()

        await self._expand_reply_threads(page, ctx)

    async def _expand_reply_threads(self, page: Page, ctx: CrawlContext) -> int:
        """Open every collapsed "Xem N cau tra loi" / "Xem N phan hoi" thread.

        Measured directly against a live post: Facebook Vietnamese renders
        these with *two different words* for "reply" depending on context
        ("cau tra loi" and "phan hoi"), and this adapter previously made no
        attempt at all to open them - not even once. Their content is
        otherwise invisible to both the network harvester and the SSR JSON,
        so skipping this silently drops every nested reply on the post.
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

    async def _dom_post_links(self, page: Page) -> list[tuple[str, str]]:
        try:
            hrefs: list[str] = await page.evaluate(
                f"() => Array.from(document.querySelectorAll('{POST_LINK_SELECTOR}'))"
                ".map((anchor) => anchor.href || '')"
            )
        except PlaywrightError:
            return []

        links: list[tuple[str, str]] = []
        seen: set[str] = set()
        for href in hrefs or []:
            identity = _identity_from_url(href)
            if not identity or identity in seen:
                continue
            seen.add(identity)
            links.append((_clean_url(href), identity))
        return links

    async def _shows_login_wall(self, page: Page) -> bool:
        if "/login" in urlparse(page.url).path:
            return True
        try:
            return await page.locator('input[name="pass"], input[id="pass"]').count() > 0
        except PlaywrightError:  # pragma: no cover
            return False

    def _post_from_story(self, story: dict[str, Any], ref: PostRef) -> RawPost:
        content = normalize_text(str(dig(story, "message", "text") or ""))
        actor = _first_actor(story)
        username = _author_username(story) or ref.author_username or "unknown"

        author = AuthorRef(
            platform=self.platform,
            username=username,
            external_id=str(actor.get("id")) if actor.get("id") else None,
            display_name=actor.get("name") or None,
            avatar_url=dig(actor, "profile_picture", "uri"),
        )
        identity = _story_identity(story) or ref.external_id

        return RawPost(
            platform=self.platform,
            external_id=identity,
            url=_story_url(story) or ref.url,
            author=author,
            content=content,
            shortcode=identity,
            posted_at=epoch_to_datetime(story.get("creation_time") or story.get("created_time")),
            metrics=_metrics_from_story(story),
            hashtags=extract_hashtags(content),
            mentions=extract_mentions(content),
            raw={"post_id": story.get("post_id"), "__typename": story.get("__typename")},
        )

    def _comment_from_node(
        self, node: dict[str, Any], post_external_id: str, root_author: str
    ) -> RawComment:
        author_node = node.get("author") or {}
        username = _username_from_url(str(author_node.get("url") or "")) or str(
            author_node.get("name") or "unknown"
        )
        author = AuthorRef(
            platform=self.platform,
            username=username,
            external_id=str(author_node.get("id")) if author_node.get("id") else None,
            display_name=author_node.get("name") or None,
        )
        parent = dig(node, "parent_comment", "id") or dig(node, "parent_id")

        return RawComment(
            platform=self.platform,
            external_id=str(node.get("id")),
            post_external_id=post_external_id,
            author=author,
            content=normalize_text(str(dig(node, "body", "text") or "")),
            parent_external_id=str(parent) if parent else None,
            posted_at=epoch_to_datetime(node.get("created_time")),
            like_count=_as_int(
                dig(node, "feedback", "reactors", "count")
                or dig(node, "feedback", "reaction_count", "count")
            ),
            reply_count=_as_int(dig(node, "feedback", "replies_fields", "total_count")),
            depth=2 if parent else 1,
            is_author_reply=bool(root_author) and username.lower() == root_author.lower(),
        )


# ------------------------------------------------------------------- predicates


def _is_story_node(node: dict[str, Any]) -> bool:
    if not isinstance(node, dict):
        return False
    if not (node.get("post_id") or node.get("__typename") == "Story"):
        return False
    return (
        isinstance(node.get("feedback"), dict)
        or isinstance(node.get("message"), dict)
        or isinstance(node.get("actors"), list)
    )


def _is_comment_node(node: dict[str, Any]) -> bool:
    if not isinstance(node, dict) or not node.get("id"):
        return False
    body = node.get("body")
    if not isinstance(body, dict) or not isinstance(body.get("text"), str):
        return False
    return isinstance(node.get("author"), dict)


# ------------------------------------------------------------------- extraction


def _story_identity(node: dict[str, Any]) -> str | None:
    for key in ("post_id", "id"):
        value = node.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _story_url(node: dict[str, Any]) -> str | None:
    for key in ("wwwURL", "url", "permalink_url"):
        value = node.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return _clean_url(value)
    return None


def _first_actor(node: dict[str, Any]) -> dict[str, Any]:
    actors = node.get("actors")
    if isinstance(actors, list) and actors and isinstance(actors[0], dict):
        return actors[0]
    owner = node.get("owning_profile") or node.get("actor")
    return owner if isinstance(owner, dict) else {}


def _author_username(node: dict[str, Any]) -> str | None:
    actor = _first_actor(node)
    return _username_from_url(str(actor.get("url") or "")) or (actor.get("name") or None)


def _username_from_url(url: str) -> str | None:
    if not url:
        return None
    path = urlparse(url).path.strip("/")
    if not path or "/" in path:
        return None
    return path or None


def _identity_from_url(url: str) -> str | None:
    for pattern in _POST_ID_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)
    return None


def _clean_url(url: str) -> str:
    """Drop tracking query strings so the same post yields one stable URL."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}" if parsed.scheme else url


def _metrics_from_story(node: dict[str, Any]) -> Metrics:
    feedback = node.get("feedback") if isinstance(node.get("feedback"), dict) else {}
    likes = dig(feedback, "reaction_count", "count")
    if likes is None:
        likes = dig(feedback, "reactors", "count")
    comments = dig(feedback, "total_comment_count")
    if comments is None:
        comments = dig(feedback, "comment_rendering_instance", "comments", "total_count")
    if comments is None:
        comments = dig(feedback, "comment_count", "total_count")
    shares = dig(feedback, "share_count", "count")
    if shares is None:
        shares = dig(feedback, "reshare_count", "count")

    metrics = Metrics(
        likes=_as_int(likes),
        comments=_as_int(comments),
        shares=_as_int(shares),
        views=_as_int(dig(feedback, "video_view_count")),
        source=MetricSource.API,
        approximate=False,
    )
    return metrics


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
