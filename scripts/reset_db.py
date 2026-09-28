#!/usr/bin/env python
"""Drop everything in the development database and rebuild it from migrations.

This destroys all data: businesses, bank links, categories, transactions and
their history. Linked banks go with it — re-link through Teller Connect
afterwards, since the stored access tokens are gone.

Guarded two ways, because there is no undo: it refuses a non-local host, and it
requires ``--yes`` (``make reset-db`` passes it).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from urllib.parse import urlsplit, urlunsplit

import asyncpg

from books.config import get_settings

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="Required. There is no undo.")
    args = parser.parse_args()

    url = urlsplit(get_settings().database_url)
    host, database = url.hostname or "localhost", (url.path or "/").lstrip("/")

    if host not in LOCAL_HOSTS:
        print(
            f"Refusing to reset {database!r} on {host!r}: that is not a local database.\n"
            "Resetting a managed database (Supabase, production) is not something "
            "this script will do for you.",
            file=sys.stderr,
        )
        return 1

    if not args.yes:
        print(
            f"This would destroy every row in {database!r} on {host}:{url.port or 5432}.\n"
            "Re-run with --yes, or use `make reset-db`.",
            file=sys.stderr,
        )
        return 1

    # asyncpg speaks plain postgres:// — drop SQLAlchemy's +asyncpg marker.
    connection = await asyncpg.connect(urlunsplit(url._replace(scheme="postgresql")))
    try:
        # Dropping the schema takes the tables, the enum types and Alembic's
        # own version table together, so the rebuild starts from nothing —
        # a table-by-table TRUNCATE would leave a stale schema behind.
        await connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    finally:
        await connection.close()

    print(f"Dropped everything in {database} on {host}:{url.port or 5432}.")
    print("Now run `make migrate` to rebuild the schema.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
