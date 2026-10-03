"""Token renewal, which is the one place a bug costs the connection itself.

Fintable replaces the refresh token on every use and invalidates the old one
immediately. So the rule is narrow and absolute: the rotation must be durable
before the access token beside it is used for anything. These tests assert the
ordering, not just the outcome.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from books.core import repository as repo
from books.core import sync as sync_service
from books.core.errors import ValidationError
from books.providers.base import TokenSet
from books.security import crypto

pytestmark = pytest.mark.db


class FakeOAuthProvider:
    """Records every refresh, so double-rotation is visible."""

    name = "fake"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def refresh_tokens(self, refresh_token: str) -> TokenSet:
        self.calls.append(refresh_token)
        n = len(self.calls)
        return TokenSet(
            access_token=f"access-{n}",
            refresh_token=f"refresh-{n}",
            expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
        )


@pytest.fixture
def oauth_provider(monkeypatch):
    provider = FakeOAuthProvider()
    monkeypatch.setattr(sync_service, "get_provider", lambda name: provider)
    return provider


async def _item(session, business, *, expires_in: timedelta | None):
    connection = await repo.create_connection(
        session,
        business=business,
        provider="fake",
        provider_ref="ws-1",
        access_token_encrypted=crypto.encrypt("access-0"),
        refresh_token_encrypted=crypto.encrypt("refresh-0"),
        token_expires_at=(datetime.now(tz=UTC) + expires_in) if expires_in else None,
        institution_name="Test Bank",
    )
    await session.commit()
    return connection


async def test_a_live_token_is_used_without_refreshing(session, business, oauth_provider):
    """Refreshing needlessly would rotate — and burn a perfectly good token."""
    connection = await _item(session, business, expires_in=timedelta(hours=1))
    assert await sync_service.usable_access_token(session, connection=connection) == "access-0"
    assert oauth_provider.calls == []


async def test_an_expired_token_is_renewed(session, business, oauth_provider):
    connection = await _item(session, business, expires_in=timedelta(minutes=-1))
    assert await sync_service.usable_access_token(session, connection=connection) == "access-1"
    assert oauth_provider.calls == ["refresh-0"]


async def test_a_token_about_to_expire_is_renewed_early(session, business, oauth_provider):
    """A long sync must not die between pages holding a token that just lapsed."""
    connection = await _item(session, business, expires_in=timedelta(minutes=1))
    assert await sync_service.usable_access_token(session, connection=connection) == "access-1"


async def test_the_rotated_refresh_token_is_persisted(session, business, oauth_provider):
    """The heart of it: the new refresh token *is* the connection now."""
    connection = await _item(session, business, expires_in=timedelta(minutes=-1))
    await sync_service.usable_access_token(session, connection=connection)

    # Drop the identity map so this reads the database rather than the copy
    # already in memory — otherwise it would pass even if nothing was written.
    session.expunge_all()
    stored = await repo.get_connection(session, connection.id)
    assert crypto.decrypt(stored.refresh_token_encrypted) == "refresh-1"
    assert crypto.decrypt(stored.access_token_encrypted) == "access-1"
    assert stored.token_expires_at > datetime.now(tz=UTC)


async def test_the_rotation_is_committed_not_merely_staged(
    session, business, oauth_provider, engine
):
    """A crash after a refresh must not be able to lose the new token.

    Read back over a *separate connection*, which by definition can only see
    committed data. Checking through the same session would pass on work that
    was merely staged — and staged work is exactly what a crash discards,
    leaving the server holding refresh-1 and us holding nothing.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    connection = await _item(session, business, expires_in=timedelta(minutes=-1))
    await sync_service.usable_access_token(session, connection=connection)

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as other:
        stored = await other.scalar(
            text("SELECT refresh_token_encrypted FROM connections WHERE id = :id"),
            {"id": connection.id},
        )
    assert crypto.decrypt(stored) == "refresh-1"


async def test_a_second_call_reuses_the_fresh_token_rather_than_rotating_again(
    session, business, oauth_provider
):
    """Two syncs in a row must not burn two rotations."""
    connection = await _item(session, business, expires_in=timedelta(minutes=-1))
    first = await sync_service.usable_access_token(session, connection=connection)
    second = await sync_service.usable_access_token(session, connection=connection)
    assert first == second == "access-1"
    assert oauth_provider.calls == ["refresh-0"]


async def test_a_non_expiring_token_is_left_alone(session, business, oauth_provider):
    """Not every provider expires. A null expiry means nothing to renew."""
    connection = await _item(session, business, expires_in=None)
    assert await sync_service.usable_access_token(session, connection=connection) == "access-0"
    assert oauth_provider.calls == []


async def test_an_item_with_no_token_at_all_is_a_clear_error(session, business, oauth_provider):
    connection = await repo.create_connection(
        session,
        business=business,
        provider="fake",
        provider_ref="ws-2",
        access_token_encrypted=None,
        institution_name="Broken",
    )
    await session.commit()
    with pytest.raises(ValidationError, match="no stored access token"):
        await sync_service.usable_access_token(session, connection=connection)
