"""Copy an existing SQLite crawl database into PostgreSQL.

    python scripts/migrate_sqlite_to_postgres.py [path/to/social_crawler.sqlite3]

Only columns present in *both* schemas are copied, so a SQLite file written
before a column was added still migrates - the new columns simply take their
defaults.

Two ordering problems are handled explicitly:

* Tables are copied parent-first so foreign keys always resolve.
* ``comments.parent_id`` points at ``comments``; rows are inserted with it
  nulled and the links are restored afterwards, because a reply can have a
  lower id than the comment it answers.

Identity sequences are re-synced at the end; without that the first insert
after a migration collides with a copied id.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging, get_logger  # noqa: E402
from app.db.models import Base  # noqa: E402

logger = get_logger("migrate")

# Parent-first: every table's foreign keys point only at tables above it.
COPY_ORDER = [
    "crawl_jobs",
    "job_targets",
    "authors",
    "posts",
    "comments",
    "job_posts",
    "post_metric_snapshots",
]

# Tables whose primary key comes from an identity/serial sequence.
SEQUENCE_TABLES = [
    "crawl_jobs",
    "job_targets",
    "authors",
    "posts",
    "comments",
    "post_metric_snapshots",
]


async def sqlite_columns(connection, table: str) -> list[str]:
    rows = await connection.execute(text(f"PRAGMA table_info({table})"))
    return [row[1] for row in rows.all()]


async def main() -> int:
    configure_logging("INFO")
    settings = get_settings()

    if "postgresql" not in settings.database_url:
        print("SC_DATABASE_URL must point at PostgreSQL before migrating.")
        return 1

    source_path = Path(sys.argv[1]) if len(sys.argv) > 1 else settings.data_dir / "social_crawler.sqlite3"
    if not source_path.exists():
        print(f"No SQLite database at {source_path} - nothing to migrate.")
        return 1

    source = create_async_engine(f"sqlite+aiosqlite:///{source_path.as_posix()}")
    target = create_async_engine(settings.database_url)

    copied: dict[str, int] = {}
    try:
        async with target.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with source.connect() as src, target.begin() as dst:
            for table in COPY_ORDER:
                model_columns = {c.name for c in Base.metadata.tables[table].columns}
                available = await sqlite_columns(src, table)
                columns = [c for c in available if c in model_columns]
                if not columns:
                    logger.warning("%s: no matching columns, skipped", table)
                    continue

                # Read and write through the Table objects, not raw SQL: they
                # carry the column types, so SQLite's integers-for-booleans and
                # text-for-timestamps are converted on the way out and bound
                # correctly on the way in. asyncpg rejects the raw forms.
                sa_table = Base.metadata.tables[table]
                selected = [sa_table.c[name] for name in columns]
                rows = (await src.execute(select(*selected))).mappings().all()
                if not rows:
                    copied[table] = 0
                    continue

                payload = [dict(row) for row in rows]
                if table == "comments":
                    # Restored after every row exists (see module docstring).
                    for item in payload:
                        item["parent_id"] = None

                await dst.execute(
                    pg_insert(sa_table).on_conflict_do_nothing(), payload
                )
                copied[table] = len(payload)
                logger.info("%-22s %4d rows", table, len(payload))

            # Re-link replies now that every comment row is present.
            relinked = await dst.execute(
                text(
                    "UPDATE comments c SET parent_id = p.id "
                    "FROM comments p "
                    "WHERE c.parent_external_id IS NOT NULL "
                    "  AND c.parent_id IS NULL "
                    "  AND p.platform = c.platform "
                    "  AND p.external_id = c.parent_external_id"
                )
            )
            logger.info("re-linked %s nested comments", relinked.rowcount)

            for table in SEQUENCE_TABLES:
                await dst.execute(
                    text(
                        "SELECT setval(pg_get_serial_sequence(:t, 'id'), "
                        "COALESCE((SELECT MAX(id) FROM " + table + "), 1), true)"
                    ),
                    {"t": table},
                )
            logger.info("identity sequences re-synced")
    finally:
        await source.dispose()
        await target.dispose()

    print("\nMigrated:")
    for table in COPY_ORDER:
        print(f"  {table:24} {copied.get(table, 0):5} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
