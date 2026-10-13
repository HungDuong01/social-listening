"""Export crawled data to XLSX / CSV / JSON."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Iterable

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.utils import strip_accents, utcnow
from app.db.models import Comment, Post
from app.db.repositories import PostFilters, QueryRepository

logger = get_logger(__name__)

FONT_NAME = "Arial"
_HEADER_FILL = PatternFill("solid", fgColor="2F5597")
_HEADER_FONT = Font(name=FONT_NAME, bold=True, color="FFFFFF", size=11)
_BASE_FONT = Font(name=FONT_NAME, size=10)
_WRAP = Alignment(wrap_text=True, vertical="top")
_HEADER_ALIGN = Alignment(wrap_text=True, vertical="center", horizontal="center")
_THIN = Side(style="thin", color="C9C9C9")
_THICK = Side(style="medium", color="2F5597")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_BANDS = ("FFFFFF", "EAF1FB")

# Column order mirrors the reference workbook the user works from, with
# Platform inserted so one report can hold every network at once.
_REPORT_HEADERS = (
    "STT",
    "Platform",
    "Account",
    "Post Text",
    "Comments",
    "Number of Likes",
    "Number of Reposts",
    "Number of Shares",
    "Comment Likes",
    "Note",
    "Post URL",
    "Views",
    "Comments Count (platform)",
    "Comments Collected",
)

_SIMPLE_HEADERS = (
    "Platform",
    "Account",
    "Post Text",
    "Comments",
    "Number of Likes",
    "Number of Reposts",
    "Number of Shares",
)

_SUMMARY_HEADERS = (
    "STT",
    "Platform",
    "Account",
    "Post Text (rút gọn)",
    "Views",
    "Number of Likes",
    "Number of Reposts",
    "Number of Shares",
    "Comments Count (platform)",
    "Comments Collected",
    "Metrics Source",
    "Post URL",
)


class ExportService:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    async def export(
        self,
        session: AsyncSession,
        *,
        filters: PostFilters,
        fmt: str = "xlsx",
        label: str = "export",
        limit: int = 5_000,
    ) -> Path:
        repository = QueryRepository(session)
        posts = await repository.posts_for_export(filters, limit=limit)
        post_ids = [post.id for post in posts]
        comments = await repository.comments_for_posts(post_ids)
        records = await repository.job_post_records(post_ids, job_id=filters.job_id)

        target = self._settings.exports_dir / self._build_filename(
            label=label, filters=filters, posts=posts, fmt=fmt
        )

        if fmt == "xlsx":
            _write_xlsx(target, posts, comments, records)
        elif fmt == "csv":
            _write_csv(target, posts, comments, records)
        elif fmt == "json":
            _write_json(target, posts, comments, records)
        else:
            raise ValueError(f"Unsupported export format: {fmt}")

        logger.info("Exported %s posts to %s", len(posts), target)
        return target

    # ------------------------------------------------------------ file naming

    def _build_filename(
        self, *, label: str, filters: PostFilters, posts: list[Post], fmt: str
    ) -> str:
        """``<keyword>_<platform>_<date>_<time>.<fmt>``

        The timestamp is local, not UTC: the name is read by a person who
        exported the file at a wall-clock time, and a UTC name is seven hours
        out here - the previous scheme produced "044533" for an 11:45 export.
        """
        keyword = _slugify(label or filters.keyword or "", fallback="tat-ca")
        platform = _slugify(self._platform_tag(filters, posts), fallback="khong-ro")
        moment = self._local_now()
        return f"{keyword}_{platform}_{moment:%Y-%m-%d}_{moment:%H%M%S}.{fmt}"

    @staticmethod
    def _platform_tag(filters: PostFilters, posts: list[Post]) -> str:
        """Which networks this file actually contains.

        Read from the exported rows rather than the filter: most exports are
        unfiltered, so the filter would say nothing while the data says
        exactly which platforms are inside.
        """
        if filters.platform is not None:
            return filters.platform.value
        found = sorted({post.platform for post in posts if post.platform})
        if not found:
            return "trong"
        return "-".join(found)

    def _local_now(self) -> datetime:
        """Now, in the timezone the application is configured for.

        Falls back to UTC when the zone database is unavailable - on Windows
        zoneinfo needs the ``tzdata`` package, and a missing one must never
        cost the user their export.
        """
        moment = utcnow()
        name = (self._settings.timezone or "").strip()
        if not name:
            return moment
        try:
            return moment.astimezone(ZoneInfo(name))
        except Exception as error:  # noqa: BLE001 - any zoneinfo failure
            logger.warning(
                "Timezone %r unavailable (%s); naming the export in UTC", name, error
            )
            return moment


# ----------------------------------------------------------------------- xlsx


def _write_xlsx(
    target: Path,
    posts: list[Post],
    comments: dict[int, list[Comment]],
    records: dict[int, Any] | None = None,
) -> None:
    records = records or {}
    workbook = Workbook()

    report = workbook.active
    report.title = "Report"
    _write_header(report, _REPORT_HEADERS)

    row = 2
    for index, post in enumerate(posts, start=1):
        post_comments = comments.get(post.id, [])
        rows = post_comments or [None]
        fill = PatternFill("solid", fgColor=_BANDS[index % 2])
        record = records.get(post.id)
        note = _note_text(record)
        collected = _collected(record, post)
        start_row = row

        for offset, comment in enumerate(rows):
            first = offset == 0
            values = (
                index if first else None,
                post.platform if first else None,
                _username(post) if first else None,
                post.content if first else None,
                _comment_cell(comment),
                post.likes if first else None,
                post.reposts if first else None,
                post.shares if first else None,
                comment.like_count if comment else None,
                note if first else None,
                post.url if first else None,
                post.views if first else None,
                post.comments_count if first else None,
                collected if first else None,
            )
            for column, value in enumerate(values, start=1):
                cell = report.cell(row=row + offset, column=column, value=value)
                cell.font = _BASE_FONT
                cell.alignment = _WRAP
                cell.border = _BORDER
                cell.fill = fill

        end_row = start_row + len(rows) - 1
        if end_row > start_row:
            # Everything except the two per-comment columns (5 and 9).
            for column in (1, 2, 3, 4, 6, 7, 8, 10, 11, 12, 13, 14):
                report.merge_cells(
                    start_row=start_row, start_column=column, end_row=end_row, end_column=column
                )
                report.cell(row=start_row, column=column).alignment = _WRAP

        for column in range(1, len(_REPORT_HEADERS) + 1):
            cell = report.cell(row=start_row, column=column)
            cell.border = Border(left=_THIN, right=_THIN, top=_THICK, bottom=cell.border.bottom)

        row = end_row + 1

    _set_widths(report, (5, 11, 18, 40, 52, 12, 12, 12, 12, 34, 32, 10, 16, 14))
    report.freeze_panes = "A2"

    _write_simple_sheet(workbook.create_sheet("Report (Simple)"), posts, comments)
    _write_summary_by_post(workbook.create_sheet("Summary"), posts, records)
    _write_comments_sheet(workbook.create_sheet("Comments"), posts, comments)
    _write_notes_sheet(workbook.create_sheet("Notes"), posts, records)

    target.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(target)


def _write_notes_sheet(sheet: Any, posts: list[Post], records: dict[int, Any]) -> None:
    """Methodology + the caveats attached to this particular crawl."""
    sheet.column_dimensions["A"].width = 110
    approximate = sum(1 for post in posts if post.metrics_approximate)
    by_platform: dict[str, int] = {}
    for post in posts:
        by_platform[post.platform] = by_platform.get(post.platform, 0) + 1

    lines = [
        "PHUONG PHAP / METHODOLOGY",
        "",
        f"Xuat luc: {utcnow().isoformat(timespec='seconds')}",
        f"Tong so bai: {len(posts)}",
        "Theo nen tang: " + (", ".join(f"{k}={v}" for k, v in sorted(by_platform.items())) or "-"),
        "",
        "CACH DOC SO LIEU",
        "- O metric de TRONG = nen tang khong cung cap so do. Khac han voi 0.",
        "- 'Metrics Source' = api  -> so nguyen chinh xac, lay tu JSON cua chinh nen tang.",
        "- 'Metrics Source' = dom  -> doc tu text da render, BI LAM TRON (vd 1.9K).",
        f"- So bai co so lieu lam tron trong bao cao nay: {approximate}",
        "",
        "- 'Comments Count (platform)'  = so comment NEN TANG cong bo.",
        "- 'Comments Collected'         = so comment THUC SU thu thap duoc.",
        "  Hai so nay lech nhau la binh thuong: nen tang loc bot comment, an bot reply,",
        "  hoac gioi han max_comments_per_post da chan lai. Cot 'Note' giai thich tung truong hop.",
        "",
        "GHI CHU THEO TUNG BAI",
    ]
    seen = False
    for index, post in enumerate(posts, start=1):
        note = _note_text(records.get(post.id))
        if note:
            seen = True
            lines.append(f"  [{index}] {post.platform} / {_username(post)}: {note}")
    if not seen:
        lines.append("  (khong co ghi chu nao)")

    for offset, line in enumerate(lines, start=1):
        cell = sheet.cell(row=offset, column=1, value=line)
        cell.font = Font(name=FONT_NAME, size=10, bold=(offset == 1))
        cell.alignment = Alignment(wrap_text=True, vertical="top")


def _write_simple_sheet(
    sheet: Any, posts: list[Post], comments: dict[int, list[Comment]]
) -> None:
    """The stripped-down view: who posted what, and what people replied."""
    _write_header(sheet, _SIMPLE_HEADERS)
    row = 2
    for index, post in enumerate(posts, start=1):
        rows = comments.get(post.id, []) or [None]
        fill = PatternFill("solid", fgColor=_BANDS[index % 2])
        start_row = row

        for offset, comment in enumerate(rows):
            first = offset == 0
            values = (
                post.platform if first else None,
                _username(post) if first else None,
                post.content if first else None,
                _comment_cell(comment),
                post.likes if first else None,
                post.reposts if first else None,
                post.shares if first else None,
            )
            for column, value in enumerate(values, start=1):
                cell = sheet.cell(row=row + offset, column=column, value=value)
                cell.font = _BASE_FONT
                cell.alignment = _WRAP
                cell.border = _BORDER
                cell.fill = fill

        end_row = start_row + len(rows) - 1
        if end_row > start_row:
            for column in (1, 2, 3, 5, 6, 7):
                sheet.merge_cells(
                    start_row=start_row, start_column=column, end_row=end_row, end_column=column
                )
                sheet.cell(row=start_row, column=column).alignment = _WRAP
        for column in range(1, len(_SIMPLE_HEADERS) + 1):
            cell = sheet.cell(row=start_row, column=column)
            cell.border = Border(left=_THIN, right=_THIN, top=_THICK, bottom=cell.border.bottom)
        row = end_row + 1

    _set_widths(sheet, (11, 18, 46, 58, 13, 13, 13))
    sheet.freeze_panes = "A2"


def _write_summary_by_post(sheet: Any, posts: list[Post], records: dict[int, Any]) -> None:
    """One row per post - the scan-and-filter view, not a per-platform total."""
    _write_header(sheet, _SUMMARY_HEADERS)
    for index, post in enumerate(posts, start=1):
        record = records.get(post.id)
        short = (post.content or "").split("\n")[0]
        if len(short) > 80:
            short = short[:77] + "..."
        _write_row(
            sheet,
            index + 1,
            (
                index,
                post.platform,
                _username(post),
                short,
                post.views,
                post.likes,
                post.reposts,
                post.shares,
                post.comments_count,
                _collected(record, post),
                post.metrics_source,
                post.url,
            ),
        )
    _set_widths(sheet, (5, 11, 18, 46, 10, 12, 12, 12, 18, 16, 14, 34))
    sheet.freeze_panes = "A2"
    if posts:
        sheet.auto_filter.ref = f"A1:L{len(posts) + 1}"


def _note_text(record: Any) -> str:
    if record is None:
        return ""
    notes = list(getattr(record, "notes", None) or [])
    if getattr(record, "comments_truncated", False):
        notes.append("dừng ở giới hạn max_comments_per_post")
    return " | ".join(str(note) for note in notes)


def _collected(record: Any, post: Post) -> int:
    """Comments collected for this post, preferring the per-run figure.

    A zero on the run record is treated as "not recorded" rather than "none
    found": rows migrated from the older SQLite schema predate that column and
    all carry 0, which would otherwise report every migrated post as having
    collected nothing. The post-level counter is accurate in that case, and
    when a run genuinely collected nothing both values are 0 anyway.
    """
    per_run = getattr(record, "comments_collected", 0) or 0
    return int(per_run) if per_run else int(post.comments_collected or 0)



def _write_comments_sheet(
    sheet: Any, posts: list[Post], comments: dict[int, list[Comment]]
) -> None:
    headers = (
        "Platform",
        "Post Account",
        "Post URL",
        "Comment Author",
        "Comment",
        "Likes",
        "Replies",
        "Depth",
        "Author Reply",
        "Posted At",
    )
    _write_header(sheet, headers)
    row = 2
    for post in posts:
        for comment in comments.get(post.id, []):
            values = (
                post.platform,
                _username(post),
                post.url,
                _username(comment),
                comment.content,
                comment.like_count,
                comment.reply_count,
                comment.depth,
                "yes" if comment.is_author_reply else "no",
                _naive(comment.posted_at),
            )
            _write_row(sheet, row, values)
            row += 1
    _set_widths(sheet, (12, 18, 34, 20, 60, 10, 10, 8, 14, 18))
    sheet.freeze_panes = "A2"



def _write_header(sheet: Any, headers: Iterable[str]) -> None:
    for column, title in enumerate(headers, start=1):
        cell = sheet.cell(row=1, column=column, value=title)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = _HEADER_ALIGN
        cell.border = _BORDER
    sheet.row_dimensions[1].height = 28


def _write_row(sheet: Any, row: int, values: Iterable[Any]) -> None:
    for column, value in enumerate(values, start=1):
        cell = sheet.cell(row=row, column=column, value=value)
        cell.font = _BASE_FONT
        cell.alignment = _WRAP
        cell.border = _BORDER


def _set_widths(sheet: Any, widths: Iterable[int]) -> None:
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width


# ------------------------------------------------------------------ csv / json


def _write_csv(target: Path, posts: list[Post], comments: dict[int, list[Comment]],
               records: dict[int, Any] | None = None) -> None:
    records = records or {}
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(_REPORT_HEADERS)
        for index, post in enumerate(posts, start=1):
            rows = comments.get(post.id, []) or [None]
            for comment in rows:
                writer.writerow(
                    [
                        index,
                        post.platform,
                        _username(post),
                        post.content,
                        _comment_cell(comment),
                        post.likes,
                        post.comments_count,
                        post.reposts,
                        post.shares,
                        post.views,
                        comment.like_count if comment else None,
                        _username(comment) if comment else None,
                        _iso(post.posted_at),
                        post.url,
                        post.metrics_source,
                    ]
                )


def _write_json(target: Path, posts: list[Post], comments: dict[int, list[Comment]],
                records: dict[int, Any] | None = None) -> None:
    records = records or {}
    target.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "exported_at": utcnow().isoformat(),
        "post_count": len(posts),
        "posts": [
            {
                "platform": post.platform,
                "external_id": post.external_id,
                "url": post.url,
                "account": _username(post),
                "content": post.content,
                "posted_at": _iso(post.posted_at),
                "metrics": {
                    "likes": post.likes,
                    "comments": post.comments_count,
                    "reposts": post.reposts,
                    "shares": post.shares,
                    "views": post.views,
                    "source": post.metrics_source,
                    "approximate": post.metrics_approximate,
                },
                "hashtags": post.hashtags or [],
                "note": _note_text(records.get(post.id)),
                "comments_collected": _collected(records.get(post.id), post),
                "comments": [
                    {
                        "external_id": comment.external_id,
                        "author": _username(comment),
                        "content": comment.content,
                        "likes": comment.like_count,
                        "replies": comment.reply_count,
                        "depth": comment.depth,
                        "is_author_reply": comment.is_author_reply,
                        "posted_at": _iso(comment.posted_at),
                    }
                    for comment in comments.get(post.id, [])
                ],
            }
            for post in posts
        ],
    }
    target.write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------- utils


def _username(entity: Post | Comment | None) -> str:
    if entity is None:
        return ""
    author = getattr(entity, "author", None)
    return author.username if author is not None else ""


def _comment_cell(comment: Comment | None) -> str:
    if comment is None:
        return "(no public comments captured)"
    return f"{_username(comment)}: {comment.content}"


def _naive(value: datetime | None) -> datetime | None:
    """openpyxl refuses timezone-aware datetimes, so store UTC wall time."""
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _slugify(value: str, *, fallback: str = "export", limit: int = 60) -> str:
    """Filename-safe slug, with Vietnamese diacritics folded to ASCII.

    "phê la mọng mơ" has to survive as "phe-la-mong-mo": leaving the accents in
    produces names that some tools and shares mangle.
    """
    folded = strip_accents(value or "")
    cleaned = "".join(char if char.isalnum() or char in "-_" else "-" for char in folded.strip())
    collapsed = "-".join(part for part in cleaned.split("-") if part)
    return (collapsed or fallback)[:limit].lower()
