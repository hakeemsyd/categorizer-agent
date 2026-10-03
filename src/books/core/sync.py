"""Provider-agnostic sync orchestration (plan.md §5).

Pulls pages until the provider says it is done, ingests each page, and advances
the stored cursor. Nothing here is provider-specific.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from books.core import repository as repo
from books.core.errors import ProviderError, ValidationError
from books.core.ingest import IngestResult, account_map, ingest_sync_result
from books.core.models import Business, Connection
from books.logging import get_logger
from books.providers import get_provider
from books.providers.base import ItemCredentials, LinkToken
from books.security import crypto

#: Renew this far ahead of expiry so a long sync cannot die mid-walk holding a
#: token that expired between pages.
_REFRESH_HEADROOM = timedelta(minutes=5)

log = get_logger(__name__)

MAX_PAGES = 200  # guardrail against a provider that never reports has_more=False


@dataclass
class SyncSummary:
    connection_id: uuid.UUID
    pages: int = 0
    inserted: int = 0
    updated: int = 0
    removed: int = 0
    skipped_before_backfill: int = 0
    new_transaction_ids: list[uuid.UUID] = field(default_factory=list)

    @property
    def new_count(self) -> int:
        return len(self.new_transaction_ids)


def create_link_token(*, provider_name: str, user_ref: str) -> LinkToken:
    return get_provider(provider_name).create_link_token(user_ref)


async def link_connection(
    session: AsyncSession,
    *,
    business: Business,
    provider_name: str,
    public_token: str,
    backfill_start_date: date | None = None,
) -> Connection:
    """Exchange a public token, store the encrypted credentials, seed accounts."""
    provider = get_provider(provider_name)
    credentials: ItemCredentials = provider.exchange_public_token(public_token)

    existing = await repo.get_connection_by_provider_ref(
        session, provider=provider_name, provider_ref=credentials.provider_ref
    )
    if existing is not None:
        raise ValidationError(
            f"That institution is already linked to business {existing.business_id} "
            f"(connection {existing.id})."
        )

    connection = await repo.create_connection(
        session,
        business=business,
        provider=provider_name,
        provider_ref=credentials.provider_ref,
        access_token_encrypted=crypto.encrypt(credentials.access_token),
        refresh_token_encrypted=(
            crypto.encrypt(credentials.refresh_token) if credentials.refresh_token else None
        ),
        token_expires_at=credentials.expires_at,
        institution_name=credentials.institution_name,
        backfill_start_date=backfill_start_date,
    )
    await refresh_accounts(session, connection=connection)
    log.info(
        "connection.linked",
        connection_id=str(connection.id),
        business_id=str(business.id),
        provider=provider_name,
        institution=credentials.institution_name,
    )
    return connection


async def refresh_accounts(session: AsyncSession, *, connection: Connection) -> int:
    provider = get_provider(connection.provider)
    token = await usable_access_token(session, connection=connection)
    accounts = provider.get_accounts(token)
    for raw in accounts:
        await repo.upsert_account(session, connection=connection, raw=raw)
    return len(accounts)


async def sync_connection(
    session: AsyncSession,
    *,
    connection: Connection,
    backfill: bool = False,
    since: date | None = None,
) -> SyncSummary:
    """Drain the provider's sync feed for one connection.

    ``backfill=True`` restarts from ``cursor=None`` (the provider's sync API has
    no date range), and ``since`` discards anything older at ingestion time.
    """
    provider = get_provider(connection.provider)
    token = await usable_access_token(session, connection=connection)

    if since is None and backfill:
        since = connection.backfill_start_date
    if backfill and since is not None and connection.backfill_start_date is None:
        connection.backfill_start_date = since

    cursor = None if backfill else connection.cursor
    summary = SyncSummary(connection_id=connection.id)
    accounts = await account_map(session, connection)
    totals = IngestResult()

    try:
        for _ in range(MAX_PAGES):
            result = provider.sync_transactions(token, cursor)
            summary.pages += 1

            unknown = {t.account_id for t in [*result.added, *result.modified]} - accounts.keys()
            if unknown:
                # A new account appeared on the connection mid-sync.
                await refresh_accounts(session, connection=connection)
                accounts = await account_map(session, connection)

            totals.merge(
                await ingest_sync_result(
                    session, connection=connection, result=result, accounts=accounts, since=since
                )
            )
            cursor = result.next_cursor
            if not result.has_more:
                break
        else:
            log.warning(
                "sync.page_limit_reached", connection_id=str(connection.id), pages=MAX_PAGES
            )
    except ProviderError as exc:
        connection.status = "error"
        connection.last_error = str(exc)
        raise

    connection.cursor = cursor
    connection.last_synced_at = datetime.now(UTC)
    connection.status = "active"
    connection.last_error = None

    summary.inserted = totals.inserted
    summary.updated = totals.updated
    summary.removed = totals.removed
    summary.skipped_before_backfill = totals.skipped_before_backfill
    summary.new_transaction_ids = totals.new_transaction_ids

    log.info(
        "sync.completed",
        connection_id=str(connection.id),
        pages=summary.pages,
        inserted=summary.inserted,
        updated=summary.updated,
        removed=summary.removed,
        skipped_before_backfill=summary.skipped_before_backfill,
    )
    return summary


async def force_refresh(session: AsyncSession, *, connection: Connection) -> None:
    token = await usable_access_token(session, connection=connection)
    get_provider(connection.provider).force_refresh(token)


async def usable_access_token(session: AsyncSession, *, connection: Connection) -> str:
    """The connection's access token, renewed first if it is spent.

    This is the only place a refresh happens, and the ordering is the whole
    point. Fintable replaces the refresh token on every use and invalidates
    the old one immediately, so a rotation that is not committed is a lost
    connection — recoverable only by sending a human back through a browser.
    Therefore: lock the row, re-read it, refresh, **commit**, and only then
    hand the access token to a caller.

    The lock matters as much as the commit. Beat syncs hourly and a human can
    sync by hand at the same moment; two refreshes racing would each rotate,
    and whichever lost would be holding a token the server had already thrown
    away. Serializing on the row means the second waits and then finds the
    first one's work already done.
    """
    if not connection.access_token_encrypted:
        raise ValidationError(f"Connection {connection.id} has no stored access token")

    expires_at = connection.token_expires_at
    if expires_at is None or expires_at > datetime.now(tz=UTC) + _REFRESH_HEADROOM:
        return crypto.decrypt(connection.access_token_encrypted)

    provider = get_provider(connection.provider)
    refresher = getattr(provider, "refresh_tokens", None)
    if refresher is None or not connection.refresh_token_encrypted:
        # Nothing to renew with. Let the call proceed and fail against the
        # provider, which gives a far clearer error than guessing here.
        return crypto.decrypt(connection.access_token_encrypted)

    # FOR UPDATE: whoever gets here second blocks until the first commits,
    # then re-reads and finds a fresh token rather than rotating again.
    locked = (
        await session.execute(
            select(Connection).where(Connection.id == connection.id).with_for_update()
        )
    ).scalar_one()
    if (
        locked.token_expires_at
        and locked.token_expires_at > datetime.now(tz=UTC) + _REFRESH_HEADROOM
    ):
        await session.refresh(connection)
        return crypto.decrypt(locked.access_token_encrypted or "")

    tokens = refresher(crypto.decrypt(locked.refresh_token_encrypted or ""))
    locked.access_token_encrypted = crypto.encrypt(tokens.access_token)
    if tokens.refresh_token:
        locked.refresh_token_encrypted = crypto.encrypt(tokens.refresh_token)
    locked.token_expires_at = tokens.expires_at
    # Commit before returning: the new refresh token is the connection now,
    # and a crash after this point must not be able to lose it.
    await session.commit()
    await session.refresh(connection)

    log.info(
        "connection.token_refreshed", connection_id=str(connection.id), provider=connection.provider
    )
    return tokens.access_token
