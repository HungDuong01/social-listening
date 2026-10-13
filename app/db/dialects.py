"""Dialect-aware SQL construction.

The repositories rely on ``INSERT ... ON CONFLICT DO UPDATE`` for idempotent
writes. Both SQLite and PostgreSQL support it with an identical SQLAlchemy
API, but the constructor lives in a *dialect-specific* module, so importing
``sqlalchemy.dialects.sqlite.insert`` hard-wires the application to SQLite -
the statement it builds is rejected outright by any other backend.

Choosing the constructor from the session's actual bind keeps one code path
working on both, which is what lets the same repositories run against the
local SQLite file during tests and against PostgreSQL in production.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

POSTGRES = "postgresql"
SQLITE = "sqlite"

_UPSERT_BUILDERS = {POSTGRES: postgres_insert, SQLITE: sqlite_insert}


def dialect_name(session: AsyncSession) -> str:
    """Backend behind this session: ``postgresql`` or ``sqlite``."""
    return session.get_bind().dialect.name


def upsert(session: AsyncSession, table: Any) -> Any:
    """An INSERT statement that supports ``.on_conflict_do_update()``.

    Raises for any other backend rather than silently producing a statement
    without upsert semantics, which would turn re-crawls into duplicate rows.
    """
    name = dialect_name(session)
    builder = _UPSERT_BUILDERS.get(name)
    if builder is None:
        raise NotImplementedError(
            f"Upserts are not implemented for the {name!r} dialect; "
            "use PostgreSQL or SQLite."
        )
    return builder(table)


def is_postgres(session: AsyncSession) -> bool:
    return dialect_name(session) == POSTGRES
