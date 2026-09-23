"""Provider-agnostic sync orchestration (plan.md §5).

Pulls pages until the provider says it is done, ingests each page, and advances
the stored cursor. Nothing here is provider-specific.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from books.core import repository as repo
from books.core.errors import ProviderError, ValidationError
from books.core.ingest import IngestResult, account_map, ingest_sync_result
from books.core.models import Business, Item
from books.logging import get_logger
from books.providers import get_provider
from books.providers.base import ItemCredentials, LinkToken
from books.security import crypto

log = get_logger(__name__)

MAX_PAGES = 200  # guardrail against a provider that never reports has_more=False


@dataclass
class SyncSummary:
    item_id: uuid.UUID
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


async def link_item(
    session: AsyncSession,
    *,
    business: Business,
    provider_name: str,
    public_token: str,
    backfill_start_date: date | None = None,
) -> Item:
    """Exchange a public token, store the encrypted credentials, seed accounts."""
    provider = get_provider(provider_name)
    credentials: ItemCredentials = provider.exchange_public_token(public_token)

    existing = await repo.get_item_by_provider_id(
        session, provider=provider_name, provider_item_id=credentials.provider_item_id
    )
    if existing is not None:
        raise ValidationError(
            f"That institution is already linked to business {existing.business_id} "
            f"(item {existing.id})."
        )

    item = await repo.create_item(
        session,
        business=business,
        provider=provider_name,
        provider_item_id=credentials.provider_item_id,
        access_token_encrypted=crypto.encrypt(credentials.access_token),
        institution_name=credentials.institution_name,
        backfill_start_date=backfill_start_date,
    )
    await refresh_accounts(session, item=item)
    log.info(
        "item.linked",
        item_id=str(item.id),
        business_id=str(business.id),
        provider=provider_name,
        institution=credentials.institution_name,
    )
    return item


async def refresh_accounts(session: AsyncSession, *, item: Item) -> int:
    provider = get_provider(item.provider)
    token = _access_token(item)
    accounts = provider.get_accounts(token)
    for raw in accounts:
        await repo.upsert_account(session, item=item, raw=raw)
    return len(accounts)


async def sync_item(
    session: AsyncSession,
    *,
    item: Item,
    backfill: bool = False,
    since: date | None = None,
) -> SyncSummary:
    """Drain the provider's sync feed for one item.

    ``backfill=True`` restarts from ``cursor=None`` (the provider's sync API has
    no date range), and ``since`` discards anything older at ingestion time.
    """
    provider = get_provider(item.provider)
    token = _access_token(item)

    if since is None and backfill:
        since = item.backfill_start_date
    if backfill and since is not None and item.backfill_start_date is None:
        item.backfill_start_date = since

    cursor = None if backfill else item.cursor
    summary = SyncSummary(item_id=item.id)
    accounts = await account_map(session, item)
    totals = IngestResult()

    try:
        for _ in range(MAX_PAGES):
            result = provider.sync_transactions(token, cursor)
            summary.pages += 1

            unknown = {t.account_id for t in [*result.added, *result.modified]} - accounts.keys()
            if unknown:
                # A new account appeared on the item mid-sync.
                await refresh_accounts(session, item=item)
                accounts = await account_map(session, item)

            totals.merge(
                await ingest_sync_result(
                    session, item=item, result=result, accounts=accounts, since=since
                )
            )
            cursor = result.next_cursor
            if not result.has_more:
                break
        else:
            log.warning("sync.page_limit_reached", item_id=str(item.id), pages=MAX_PAGES)
    except ProviderError as exc:
        item.status = "error"
        item.last_error = str(exc)
        raise

    item.cursor = cursor
    item.last_synced_at = datetime.now(UTC)
    item.status = "active"
    item.last_error = None

    summary.inserted = totals.inserted
    summary.updated = totals.updated
    summary.removed = totals.removed
    summary.skipped_before_backfill = totals.skipped_before_backfill
    summary.new_transaction_ids = totals.new_transaction_ids

    log.info(
        "sync.completed",
        item_id=str(item.id),
        pages=summary.pages,
        inserted=summary.inserted,
        updated=summary.updated,
        removed=summary.removed,
        skipped_before_backfill=summary.skipped_before_backfill,
    )
    return summary


def force_refresh(item: Item) -> None:
    get_provider(item.provider).force_refresh(_access_token(item))


def _access_token(item: Item) -> str:
    if not item.access_token_encrypted:
        raise ValidationError(f"Item {item.id} has no stored access token")
    return crypto.decrypt(item.access_token_encrypted)
