"""Create the schema on PostgreSQL and apply the indexes it needs.

Run after ``bootstrap_postgres.sql``:

    python scripts/setup_postgres.py

The table definitions come from the ORM metadata so the schema cannot drift
from the models. The indexes below are PostgreSQL-only and deliberately kept
out of the models: SQLite cannot create them, and the models have to stay
portable so the test suite can keep running against a file database.

Every index here answers a query the application actually issues - an index
nothing queries is just write amplification.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging, get_logger  # noqa: E402
from app.db.models import Base  # noqa: E402
from app.db.session import dispose_db, get_engine  # noqa: E402

logger = get_logger("setup_postgres")

# (name, DDL, why it exists)
POSTGRES_INDEXES: list[tuple[str, str, str]] = [
    (
        "ix_posts_content_trgm",
        "CREATE INDEX IF NOT EXISTS ix_posts_content_trgm "
        "ON posts USING gin (content gin_trgm_ops)",
        "QueryRepository filters posts with lower(content) LIKE '%kw%'; "
        "without a trigram index that is always a sequential scan.",
    ),
    (
        "ix_authors_username_lower",
        "CREATE INDEX IF NOT EXISTS ix_authors_username_lower "
        "ON authors (lower(username))",
        "The author filter compares lower(username), so a plain index on the "
        "column itself can never be used.",
    ),
    (
        "ix_posts_platform_last_seen",
        "CREATE INDEX IF NOT EXISTS ix_posts_platform_last_seen "
        "ON posts (platform, last_seen_at DESC)",
        "Default post listing orders by last_seen_at and usually filters by "
        "platform.",
    ),
    (
        "ix_job_posts_post",
        "CREATE INDEX IF NOT EXISTS ix_job_posts_post ON job_posts (post_id)",
        "The primary key covers (job_id, post_id); the reverse lookup "
        "'which jobs found this post' has no index without this.",
    ),
    (
        "ix_snapshots_post_captured",
        "CREATE INDEX IF NOT EXISTS ix_snapshots_post_captured "
        "ON post_metric_snapshots (post_id, captured_at DESC)",
        "Metric history for one post is read newest-first.",
    ),
    (
        "ix_jobs_unfinished",
        "CREATE INDEX IF NOT EXISTS ix_jobs_unfinished ON crawl_jobs (id) "
        "WHERE status IN ('pending', 'running')",
        "Startup reconciliation scans for unfinished jobs only - a partial "
        "index stays tiny no matter how much history accumulates.",
    ),
    (
        "ix_comments_author",
        "CREATE INDEX IF NOT EXISTS ix_comments_author ON comments (author_id)",
        "Reports group comments by commenter.",
    ),
]


async def main() -> int:
    configure_logging("INFO")
    settings = get_settings()

    if "postgresql" not in settings.database_url:
        print("SC_DATABASE_URL does not point at PostgreSQL:")
        print(f"  {settings.database_url}")
        print("\nSet it in .env, for example:")
        print("  SC_DATABASE_URL=postgresql+asyncpg://social_crawler:"
              "PASSWORD@127.0.0.1:5432/social_crawler")
        return 1

    engine = get_engine(settings)

    async with engine.begin() as connection:
        schema = await connection.scalar(text("SELECT current_schema()"))
        version = await connection.scalar(text("SHOW server_version"))
        logger.info("Connected to PostgreSQL %s, schema %s", version, schema)

        await connection.run_sync(Base.metadata.create_all)
        logger.info("Tables created/verified: %s", len(Base.metadata.tables))

        for name, ddl, reason in POSTGRES_INDEXES:
            await connection.execute(text(ddl))
            logger.info("index %-28s %s", name, reason.split(".")[0])

    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT table_name, "
                "(SELECT count(*) FROM information_schema.columns c "
                "  WHERE c.table_name = t.table_name "
                "    AND c.table_schema = t.table_schema) AS cols "
                "FROM information_schema.tables t "
                "WHERE t.table_schema = current_schema() AND t.table_type = 'BASE TABLE' "
                "ORDER BY table_name"
            )
        )
        print("\nTables in schema:")
        for name, cols in rows.all():
            print(f"  {name:24} {cols:2} columns")

        index_rows = await connection.execute(
            text(
                "SELECT tablename, indexname FROM pg_indexes "
                "WHERE schemaname = current_schema() ORDER BY tablename, indexname"
            )
        )
        entries = index_rows.all()
        print(f"\nIndexes: {len(entries)}")

    await dispose_db()
    print("\nPostgreSQL schema is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
