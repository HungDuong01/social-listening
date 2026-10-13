"""Threads adapter.

Two lessons from crawling Threads by hand are encoded here:

1. A post page renders the thread's own replies *and* a "Related threads"
   block of unrelated posts. Those must never be recorded as comments. Both
   are filtered: the related block arrives from its own GraphQL operation
   (excluded by name), and a genuine reply always carries
   ``text_post_app_info.reply_to_author`` while a standalone post does not.
2. The rendered counters are rounded ("1.9K") and a zero counter is not drawn
   at all, so the DOM alone cannot distinguish "no reposts" from "48 reposts".
   The intercepted JSON carries exact integers and is therefore authoritative;
   the DOM is only used to fill gaps.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlencode

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from app.core.enums import MetricSource, Platform, SearchFilter
from app.core.exceptions import AuthenticationRequiredError
from app.core.utils import dig, epoch_to_datetime, extract_hashtags, extract_mentions
from app.core.utils import jitter_sleep, normalize_text, parse_compact_number
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

POST_LINK_SELECTOR = 'a[href*="/post/"]'
_POST_HREF_RE = re.compile(r"/@([A-Za-z0-9._]+)/post/([A-Za-z0-9_-]+)")

_COOKIE_DISMISS = (
    'button:has-text("Allow all cookies")',
    'button:has-text("Decline optional cookies")',
    'button:has-text("Cho phép tất cả cookie")',
    'button:has-text("Từ chối cookie không bắt buộc")',
)

# aria-labels are localised, so every probe carries its Vietnamese twin.
_METRIC_ARIA_LABELS: dict[str, tuple[str, ...]] = {
    "likes": ("Like", "Unlike", "Thích", "Bỏ thích"),
    "comments": ("Reply", "Comment", "Trả lời", "Bình luận"),
    "reposts": ("Repost", "Đăng lại"),
    "shares": ("Share", "Chia sẻ"),
}

_DOM_SNAPSHOT_JS = """
(labelGroups) => {
  const readCount = (names) => {
    for (const name of names) {
      const svg = document.querySelector(`svg[aria-label="${name}" i]`);
      if (!svg) continue;
      let node = svg.closest('[role="button"], button, a') || svg.parentElement;
      for (let hop = 0; hop < 4 && node; hop++) {
        const parent = node.parentElement;
        if (!parent) break;
        const text = (parent.innerText || '').trim();
        const line = text.split('\\n').find((part) => /\\d/.test(part));
        if (line) return line.trim();
        node = parent;
      }
      return '0';
    }
    return null;
  };

  const counts = {};
  for (const [key, names] of Object.entries(labelGroups)) counts[key] = readCount(names);

  let body = document.body ? document.body.innerText || '' : '';
  for (const marker of ['Related threads', 'Bài viết liên quan', 'Chủ đề liên quan']) {
    const index = body.indexOf(marker);
    if (index > 0) { body = body.slice(0, index); break; }
  }
  return { counts, text: body.slice(0, 20000) };
}
"""


@register_adapter
class ThreadsAdapter(PlatformAdapter):
    platform = Platform.THREADS
    display_name = "Threads"
    home_url = "https://www.threads.com"
    login_url = "https://www.threads.com/login"
    network_patterns = (r"/graphql/query", r"/api/graphql", r"/api/v1/")
    excluded_operations = ("related", "recommend", "similar", "suggested")
    requires_login = True
    session_cookie_names = ("sessionid",)
    cookie_urls = ("https://www.threads.com", "https://www.instagram.com")
    inline_json_keywords = ("like_count", "text_post_app_info")

    # ------------------------------------------------------------------ search

    def build_search_url(self, keyword: str, options: CrawlOptions) -> str:
        params: dict[str, str] = {"q": keyword, "serp_type": "default"}
        if options.search_filter is SearchFilter.RECENT:
            params["filter"] = "recent"
        return f"{self.home_url}/search?{urlencode(params)}"

    async def is_authenticated(self, page: Page) -> bool:
        """Cheap cookie check first, then the authoritative page-load check.

        Cookie presence alone is not proof: a session mid-checkpoint carries
        a live-looking cookie while the platform still refuses every page.
        Falling back to a generic "does the home page show a login wall"
        probe (the previous approach here) was measured giving a false
        positive on Instagram - cookies absent, yet the check reported
        authenticated because the logged-out home page happened not to look
        walled. ``confirm_session`` is the same authoritative check the
        login flow itself trusts before saving a session, so this stays
        consistent with what "logged in" means everywhere else in the app.
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
                    self.platform.value, "Threads search requires a logged-in session"
                )

            target = ctx.options.max_posts

            # Threads recycles off-screen post links out of the DOM as the
            # feed virtualises (confirmed live: the raw on-screen count
            # plateaued around 15 while the page kept growing well past
            # 30000px of scroll height underneath it) - a plain
            # page.locator(...).count() per round therefore never reliably
            # reflects how many distinct posts have actually been seen, and
            # a *final* one-shot DOM query after scrolling stops would only
            # catch whatever the virtualiser happens to still be holding at
            # that instant. Accumulating every round's DOM links into one
            # running, order-preserving map fixes both: the scroll's own
            # progress signal is now a true cumulative count, and nothing
            # discovered mid-scroll is lost once its element gets recycled.
            dom_links_seen: dict[str, tuple[str, str]] = {}

            async def count_items() -> int:
                for username, code in await self._dom_post_links(page):
                    dom_links_seen.setdefault(code, (username, code))
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

            documents: list[Any] = [payload.data for payload in harvester.select()]
            documents.extend(await self.inline_documents(page))
            api_nodes = collect_nodes(documents, is_media_node, key=node_identity)
            dom_links = list(dom_links_seen.values())
        finally:
            harvester.detach()

        return self._merge_refs(dom_links, api_nodes, limit=ctx.options.max_posts)

    # -------------------------------------------------------------------- post

    async def fetch_post(self, page: Page, ref: PostRef, ctx: CrawlContext) -> CrawlResult:
        harvester = self.make_harvester()
        harvester.attach(page)
        notes: list[str] = []
        try:
            await self.navigate(page, ref.url, ctx)
            if await self._shows_login_wall(page):
                raise AuthenticationRequiredError(
                    self.platform.value, "Threads post page requires a logged-in session"
                )
            await self._ensure_thread_view(page, ref, ctx)
            if ctx.options.include_comments:
                await self._load_replies(page, ctx)

            payloads = harvester.select(exclude_operations=self.excluded_operations)
            documents: list[Any] = [payload.data for payload in payloads]
            # Signed-out pages are server-rendered and issue no GraphQL calls,
            # so the embedded blobs are the only exact source available.
            documents.extend(await self.inline_documents(page))
            nodes = collect_nodes(documents, is_media_node, key=node_identity)
            snapshot = await self._dom_snapshot(page)
        finally:
            harvester.detach()

        root_node = next((node for node in nodes if node_identity(node) == ref.external_id), None)
        dom_metrics = _metrics_from_snapshot(snapshot)

        if root_node is None:
            notes.append("post JSON not intercepted - values come from the rendered page")
            post = self._post_from_dom(ref, snapshot, dom_metrics)
        else:
            post = self._post_from_node(root_node, fallback_url=ref.url)
            post.metrics = post.metrics.merged_with(dom_metrics)

        comments: list[RawComment] = []
        truncated = False
        if ctx.options.include_comments:
            comments, truncated = self._replies_from_nodes(
                nodes,
                root_node=root_node,
                root_external_id=ref.external_id,
                root_author=post.author.username,
                limit=ctx.options.max_comments_per_post,
            )
            expected = post.metrics.comments
            if not comments and expected:
                # The platform's own counter says replies exist but none were
                # served. Threads withholds them from anonymous sessions after
                # repeated visits, so record the gap instead of storing a
                # silent zero that looks like a post nobody replied to.
                notes.append(
                    f"platform reports {expected} replies but none were served "
                    "- sign in to this platform to collect them"
                )
            elif comments and expected and len(comments) < expected:
                notes.append(f"collected {len(comments)} of {expected} replies reported")

        return CrawlResult(
            post=post, comments=comments, comments_truncated=truncated, notes=notes
        )

    # ----------------------------------------------------------------- helpers

    async def _ensure_thread_view(self, page: Page, ref: PostRef, ctx: CrawlContext) -> None:
        """Land on the real thread page, not the home feed the post gets injected into.

        Confirmed live: a plain ``page.goto(post_url)`` while signed in gets
        server-redirected to ``https://www.threads.com/?injected_media_ids=[...]``,
        which the client then normalises to a bare ``/`` - the target post
        renders only as the first card of the "Dành cho bạn" feed, and zero
        reply payloads are ever fetched for it (the harvester intercepted 43
        home-feed queries and 0 reply nodes in this state). Replies load only
        once the SPA router handles a genuine in-app click on that card's own
        link - the same live test then intercepted 73 reply nodes. So: if the
        redirect happened, click the injected card back into the thread view
        the way a real visitor would.
        """
        if f"/post/{ref.external_id}" in page.url:
            return

        link = page.locator(f'a[href*="/post/{ref.external_id}"]').first
        try:
            if await link.count() == 0:
                self.logger.warning(
                    "Threads redirected %s to the home feed and the injected "
                    "card could not be found to click back into it",
                    ref.external_id,
                )
                return
            await link.click(timeout=5_000)
            await page.wait_for_url(
                lambda url: f"/post/{ref.external_id}" in url, timeout=8_000
            )
        except PlaywrightError as error:
            self.logger.warning(
                "Could not click back into the thread view for %s: %s",
                ref.external_id,
                str(error).splitlines()[0],
            )
            return
        await jitter_sleep(700, 1300)

    async def _load_replies(self, page: Page, ctx: CrawlContext) -> None:
        # No artificial cap below the configured max_scrolls, and idle_rounds
        # raised from 2 to 4: measured live on a 322-reply post, the old
        # settings stopped after collecting 74 of 334 replies served (the
        # thread was still growing new links each round when the scroll
        # quit). Same fix already proven for Facebook/Instagram threads.
        policy = ScrollPolicy(
            max_scrolls=self.settings.max_scrolls,
            idle_rounds=4,
            min_delay_ms=self.settings.scroll_delay_min_ms,
            max_delay_ms=self.settings.scroll_delay_max_ms,
        )
        await infinite_scroll(
            page,
            policy=policy,
            count_items=lambda: self.count_links(page, POST_LINK_SELECTOR),
        )
        ctx.raise_if_cancelled()

    async def _dom_post_links(self, page: Page) -> list[tuple[str, str]]:
        try:
            hrefs: list[str] = await page.evaluate(
                "() => Array.from(document.querySelectorAll('a[href*=\"/post/\"]'))"
                ".map((anchor) => anchor.getAttribute('href') || '')"
            )
        except PlaywrightError:
            return []

        ordered: list[tuple[str, str]] = []
        seen: set[str] = set()
        for href in hrefs or []:
            match = _POST_HREF_RE.search(href)
            if not match:
                continue
            username, code = match.group(1), match.group(2)
            if code in seen:
                continue
            seen.add(code)
            ordered.append((username, code))
        return ordered

    async def _dom_snapshot(self, page: Page) -> dict[str, Any]:
        try:
            return await page.evaluate(_DOM_SNAPSHOT_JS, _METRIC_ARIA_LABELS)
        except PlaywrightError:
            return {"counts": {}, "text": ""}

    async def _shows_login_wall(self, page: Page) -> bool:
        if "/login" in page.url or "/accounts/login" in page.url:
            return True
        try:
            return await page.locator('input[name="password"]').count() > 0
        except PlaywrightError:  # pragma: no cover
            return False

    def _merge_refs(
        self,
        dom_links: list[tuple[str, str]],
        api_nodes: list[dict[str, Any]],
        *,
        limit: int,
    ) -> list[PostRef]:
        """DOM order defines ranking; JSON adds the exact preview metrics."""
        by_code: dict[str, dict[str, Any]] = {}
        for node in api_nodes:
            identity = node_identity(node)
            if identity:
                by_code[identity] = node

        refs: list[PostRef] = []
        seen: set[str] = set()

        for username, code in dom_links:
            node = by_code.get(code)
            refs.append(
                PostRef(
                    platform=self.platform,
                    external_id=code,
                    url=self._post_url(username, code),
                    author_username=username,
                    rank=len(refs) + 1,
                    preview_metrics=_metrics_from_node(node) if node else Metrics(),
                    raw=node,
                )
            )
            seen.add(code)

        for code, node in by_code.items():
            if code in seen:
                continue
            username = str(dig(node, "user", "username") or "").strip()
            if not username:
                continue
            refs.append(
                PostRef(
                    platform=self.platform,
                    external_id=code,
                    url=self._post_url(username, code),
                    author_username=username,
                    rank=len(refs) + 1,
                    preview_metrics=_metrics_from_node(node),
                    raw=node,
                )
            )

        return refs[:limit]

    def _post_url(self, username: str, code: str) -> str:
        return f"{self.home_url}/@{username}/post/{code}"

    def _post_from_node(self, node: dict[str, Any], *, fallback_url: str) -> RawPost:
        author = build_author(node, self.platform)
        content = extract_text(node)
        code = node.get("code") if isinstance(node.get("code"), str) else None
        url = self._post_url(author.username, code) if code and author.username else fallback_url

        return RawPost(
            platform=self.platform,
            external_id=node_identity(node) or code or fallback_url,
            url=url,
            author=author,
            content=content,
            shortcode=code,
            posted_at=epoch_to_datetime(node.get("taken_at")),
            metrics=_metrics_from_node(node),
            media=build_media(node),
            hashtags=extract_hashtags(content),
            mentions=extract_mentions(content),
            raw=_slim(node),
        )

    def _post_from_dom(
        self, ref: PostRef, snapshot: dict[str, Any], metrics: Metrics
    ) -> RawPost:
        text = normalize_text(str(snapshot.get("text") or ""))
        content = _guess_post_text(text, ref.author_username or "")
        return RawPost(
            platform=self.platform,
            external_id=ref.external_id,
            url=ref.url,
            author=AuthorRef(platform=self.platform, username=ref.author_username or "unknown"),
            content=content,
            shortcode=ref.external_id,
            metrics=metrics,
            hashtags=extract_hashtags(content),
            mentions=extract_mentions(content),
        )

    def _replies_from_nodes(
        self,
        nodes: list[dict[str, Any]],
        *,
        root_node: dict[str, Any] | None,
        root_external_id: str,
        root_author: str,
        limit: int,
    ) -> tuple[list[RawComment], bool]:
        comments: list[RawComment] = []
        # Maps an author to their most recent reply, so a nested reply can be
        # attached to the comment it answers (Threads only tells us *who* was
        # replied to, never which specific comment).
        last_reply_by_author: dict[str, str] = {}
        # Everyone known to take part in *this* thread. A post page also renders
        # a trending feed whose entries are replies in their own threads, and
        # those carry reply_to_author too - without this set they would be
        # filed as comments on the post being crawled.
        participants: set[str] = {(root_author or "").strip().lower()}
        truncated = False

        for node in nodes:
            identity = node_identity(node)
            if not identity or identity == root_external_id or node is root_node:
                continue

            thread_info = node.get("text_post_app_info") or {}
            reply_to = thread_info.get("reply_to_author")
            if not isinstance(reply_to, dict):
                # A standalone post: recommended/related content, not a reply.
                continue

            replied_to_username = str(reply_to.get("username") or "").strip().lower()
            if replied_to_username not in participants:
                # Answers somebody who never appeared in this thread, so it
                # belongs to a different conversation rendered on the same page.
                continue

            # limit 0 means "no limit"; a plain >= test would stop at zero.
            if limit and len(comments) >= limit:
                truncated = True
                break

            author = build_author(node, self.platform)
            is_top_level = replied_to_username == (root_author or "").strip().lower()
            parent_external_id = None if is_top_level else last_reply_by_author.get(
                replied_to_username
            )

            content = extract_text(node)
            comments.append(
                RawComment(
                    platform=self.platform,
                    external_id=identity,
                    post_external_id=root_external_id,
                    author=author,
                    content=content,
                    parent_external_id=parent_external_id,
                    posted_at=epoch_to_datetime(node.get("taken_at")),
                    like_count=as_int(node.get("like_count")),
                    reply_count=as_int(thread_info.get("direct_reply_count")),
                    depth=1 if is_top_level else 2,
                    is_author_reply=author.username.lower() == (root_author or "").lower(),
                    raw=_slim(node),
                )
            )
            if author.username:
                last_reply_by_author[author.username.lower()] = identity
                participants.add(author.username.lower())

        return comments, truncated


def _metrics_from_node(node: dict[str, Any]) -> Metrics:
    thread_info = node.get("text_post_app_info") or {}
    return Metrics(
        likes=as_int(node.get("like_count")),
        comments=as_int(thread_info.get("direct_reply_count")),
        reposts=as_int(thread_info.get("repost_count")),
        shares=as_int(thread_info.get("reshare_count")),
        views=as_int(dig(node, "feedback_info", "view_count")),
        source=MetricSource.API,
        approximate=False,
    )


def _metrics_from_snapshot(snapshot: dict[str, Any]) -> Metrics:
    counts = snapshot.get("counts") or {}
    parsed = {key: parse_compact_number(counts.get(key)) for key in _METRIC_ARIA_LABELS}
    return Metrics(
        likes=parsed["likes"].value,
        comments=parsed["comments"].value,
        reposts=parsed["reposts"].value,
        shares=parsed["shares"].value,
        source=MetricSource.DOM,
        approximate=any(item.approximate for item in parsed.values()),
    )


def _guess_post_text(page_text: str, username: str) -> str:
    """Best-effort first paragraph of the root post from the rendered page."""
    lines = [line for line in page_text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if username and line.strip().lower() == username.lower():
            body = [
                candidate
                for candidate in lines[index + 1 : index + 6]
                if not _looks_like_chrome(candidate)
            ]
            if body:
                return "\n".join(body).strip()
    return "\n".join(lines[:3]).strip()


def _looks_like_chrome(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped in {"Translate", "Dịch", "Log in", "Đăng nhập"}:
        return True
    return bool(re.fullmatch(r"\d+[smhdwSMHDW]?|\d{1,2}/\d{1,2}/\d{2,4}", stripped))


def _slim(node: dict[str, Any]) -> dict[str, Any]:
    """Keep the raw record small - full GraphQL nodes are enormous."""
    keep = (
        "pk",
        "id",
        "code",
        "taken_at",
        "like_count",
        "media_type",
        "text_post_app_info",
        "caption",
    )
    slim: dict[str, Any] = {key: node[key] for key in keep if key in node}
    user = node.get("user")
    if isinstance(user, dict):
        slim["user"] = {
            key: user.get(key) for key in ("pk", "username", "full_name", "is_verified")
        }
    return slim
