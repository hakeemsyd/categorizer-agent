#!/usr/bin/env python
"""Create the ``books_test`` database beside the configured development one.

Integration tests TRUNCATE every table, so they run against a dedicated
database rather than your development one. This creates it if missing. Tests
resolve the same URL (tests/conftest.py); override with BOOKS_TEST_DATABASE_URL.
"""

from __future__ import annotations

import asyncio
import sys
from urllib.parse import urlsplit, urlunsplit

import asyncpg

from books.config import get_settings

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


async def main() -> int:
    url = urlsplit(get_settings().database_url)
    if (url.hostname or "localhost") not in LOCAL_HOSTS:
        print(
            f"Refusing to create a test database on {url.hostname!r}. "
            "Set BOOKS_TEST_DATABASE_URL explicitly for a remote server.",
            file=sys.stderr,
        )
        return 1

    target = "books_test"
    # asyncpg speaks plain postgres:// — drop SQLAlchemy's +asyncpg marker.
    admin = urlunsplit(url._replace(scheme="postgresql", path="/postgres"))

    connection = await asyncpg.connect(admin)
    try:
        exists = await connection.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", target)
        if exists:
            print(f"{target} already exists on {url.hostname}:{url.port or 5432}")
            return 0
        await connection.execute(f'CREATE DATABASE "{target}"')
    finally:
        await connection.close()

    print(f"Created {target} on {url.hostname}:{url.port or 5432}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
