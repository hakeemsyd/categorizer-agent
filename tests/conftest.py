"""Test fixtures.

Unit tests need nothing. Integration tests need a Postgres: set
``BOOKS_TEST_DATABASE_URL`` (defaults to a local ``books_test``); if it is
unreachable those tests skip rather than fail.
"""

from __future__ import annotations

import os
import socket
from collections.abc import AsyncIterator, Iterator
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

# Configure before anything imports books.config and caches Settings.
os.environ.setdefault("BOOKS_ENV", "local")
os.environ.setdefault("BOOKS_API_TOKEN", "test-token")
os.environ.setdefault("BOOKS_DEFAULT_PROVIDER", "fake")
os.environ.setdefault("BOOKS_CONFIDENCE_THRESHOLD", "0.8")
os.environ.setdefault("BOOKS_ENCRYPTION_KEY", "Sw3JhtLZtBcFqBRt0xCoPGMoAxjCqRYF9K4AghDLN1Y=")


def _configured_database_url() -> str | None:
    """How the project is configured, env var first then ``.env``.

    pydantic-settings reads ``.env`` for the app, but this has to run before
    ``books.config`` is imported, so it parses the file itself.
    """
    if os.environ.get("BOOKS_DATABASE_URL"):
        return os.environ["BOOKS_DATABASE_URL"]

    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return None
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line.startswith("BOOKS_DATABASE_URL="):
            return line.split("=", 1)[1].strip().strip("\"'") or None
    return None


def _default_test_database_url() -> str:
    """A sibling ``books_test`` beside the configured development database.

    Tests TRUNCATE every table, so this refuses to derive anything from a
    non-local host: against a remote database you must name it explicitly via
    BOOKS_TEST_DATABASE_URL, and mean it.
    """
    configured = _configured_database_url()
    if configured:
        parsed = urlsplit(configured)
        if (parsed.hostname or "localhost") in {"localhost", "127.0.0.1", "::1"}:
            return urlunsplit(parsed._replace(path="/books_test"))
    user = os.environ.get("USER", "postgres")
    return f"postgresql+asyncpg://{user}@localhost:5432/books_test"


TEST_DATABASE_URL = os.environ.get("BOOKS_TEST_DATABASE_URL") or _default_test_database_url()
os.environ["BOOKS_DATABASE_URL"] = TEST_DATABASE_URL

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from books.core import repository as repo  # noqa: E402
from books.core.models import Base  # noqa: E402
from books.core.queue import RecordingDispatcher, set_dispatcher  # noqa: E402
from books.providers import fake as fake_provider  # noqa: E402


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
async def engine():
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.begin() as connection:
            # Drop the whole schema, not Base.metadata.drop_all: drop_all only
            # knows about tables the models still declare, so a renamed or
            # deleted table survives as an orphan — and its foreign keys then
            # block dropping the tables that are left. Renaming items to
            # connections hit exactly that.
            await connection.execute(text("DROP SCHEMA public CASCADE"))
            await connection.execute(text("CREATE SCHEMA public"))
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
            await connection.run_sync(Base.metadata.create_all)
    except (ConnectionRefusedError, OSError, socket.gaierror) as exc:
        await engine.dispose()
        pytest.skip(
            f"Cannot reach the test database at {TEST_DATABASE_URL}\n"
            f"  {type(exc).__name__}: {exc}\n"
            "  Create it with `make test-db`, or point BOOKS_TEST_DATABASE_URL "
            "somewhere else.\n"
            "  Until then every database-backed test is SKIPPED, not passing."
        )
    except Exception:
        # Anything else is a real problem with the schema itself. Skipping on
        # it once hid 115 tests behind a message about an unreachable
        # database that was in fact reachable — so let it fail loudly.
        await engine.dispose()
        raise
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:
    tables = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
        await session.rollback()


@pytest.fixture
def dispatcher() -> Iterator[RecordingDispatcher]:
    recording = RecordingDispatcher()
    previous = set_dispatcher(recording)
    yield recording
    set_dispatcher(previous)


@pytest.fixture
def fake_state():
    """Reset the FakeProvider's canned dataset for each test."""
    return fake_provider.reset_fake_state(fake_provider.default_state(date(2026, 3, 1)))


@pytest.fixture
async def business(session):
    tenant = await repo.create_tenant(session, name="Hakeem")
    created = await repo.create_business(
        session, tenant_id=tenant.id, name="Coding Crafts", details="Software consultancy"
    )
    await session.commit()
    return created


@pytest.fixture
async def chart_of_accounts(session, business) -> dict[str, object]:
    """A small chart covering every account type."""
    from books.core.accounting import AccountType

    specs = [
        ("Software & Subscriptions", AccountType.EXPENSE, "SaaS tools and cloud infrastructure"),
        ("Payroll", AccountType.EXPENSE, "Wages and contractor payments"),
        ("Travel", AccountType.EXPENSE, "Flights, hotels, ground transport"),
        # Distinct from Credit Card Payable on purpose: one card issuer bills
        # both, and only the description tells them apart.
        ("Interest Expense", AccountType.EXPENSE, "Interest on cards and loans"),
        ("Consulting Revenue", AccountType.REVENUE, "Client payments for services"),
        ("Transfers Between Accounts", AccountType.ASSET, "Movement between own accounts"),
        ("Credit Card Payable", AccountType.LIABILITY, "Company card balance"),
        ("Owner Draw", AccountType.EQUITY, "Money the owner took out"),
    ]
    categories = {
        name: await repo.create_category(
            session,
            business=business,
            name=name,
            account_type=account_type,
            description=description,
        )
        for name, account_type, description in specs
    }
    await session.commit()
    return categories


@pytest.fixture
async def linked_connection(session, business, fake_state):
    from books.core.sync import link_connection

    connection = await link_connection(
        session, business=business, provider_name="fake", public_token="pt-1"
    )
    await session.commit()
    return connection


@pytest.fixture
async def api_client(session, dispatcher):
    """httpx client bound to the FastAPI app, sharing the test session."""
    import httpx

    from books.faces.api.deps import get_session
    from books.faces.api.main import create_app

    app = create_app()

    async def _session_override():
        yield session

    app.dependency_overrides[get_session] = _session_override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer test-token"},
    ) as client:
        yield client
