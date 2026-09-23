"""Turn provider-shaped transactions into rows, provider-agnostically.

Takes :class:`books.providers.base.RawTransaction` — never a vendor payload —
so a new adapter needs no changes here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from books.core import repository as repo
from books.core.models import Account, Item
from books.logging import get_logger
from books.providers.base import RawTransaction, SyncResult

log = get_logger(__name__)


@dataclass
class IngestResult:
    inserted: int = 0
    updated: int = 0
    removed: int = 0
    skipped_before_backfill: int = 0
    skipped_unknown_account: int = 0
    new_transaction_ids: list[uuid.UUID] = field(default_factory=list)

    def merge(self, other: IngestResult) -> IngestResult:
        self.inserted += other.inserted
        self.updated += other.updated
        self.removed += other.removed
        self.skipped_before_backfill += other.skipped_before_backfill
        self.skipped_unknown_account += other.skipped_unknown_account
        self.new_transaction_ids.extend(other.new_transaction_ids)
        return self


async def account_map(session: AsyncSession, item: Item) -> dict[str, Account]:
    accounts = await repo.list_accounts(session, business_id=item.business_id)
    return {
        a.provider_account_id: a for a in accounts if a.item_id == item.id and a.provider_account_id
    }


async def ingest_sync_result(
    session: AsyncSession,
    *,
    item: Item,
    result: SyncResult,
    accounts: dict[str, Account],
    since: date | None = None,
) -> IngestResult:
    """Write one page of a provider sync.

    ``since`` implements the bounded backfill from plan.md §5: the provider has
    no date-range parameter, so the boundary is enforced here, at ingestion.
    """
    out = IngestResult()

    for raw in [*result.added, *result.modified]:
        if since and raw.date < since:
            out.skipped_before_backfill += 1
            continue

        account = accounts.get(raw.account_id)
        if account is None:
            out.skipped_unknown_account += 1
            log.warning(
                "ingest.unknown_account",
                item_id=str(item.id),
                provider_account_id=raw.account_id,
            )
            continue

        transaction_id, is_new = await repo.upsert_transaction(
            session, values=_to_values(item, account, raw)
        )
        if is_new:
            out.inserted += 1
            out.new_transaction_ids.append(transaction_id)
        else:
            out.updated += 1

    out.removed = await repo.delete_transactions_by_provider_ids(
        session, item_id=item.id, provider_transaction_ids=result.removed
    )
    return out


def _to_values(item: Item, account: Account, raw: RawTransaction) -> dict:
    return {
        "tenant_id": item.tenant_id,
        "business_id": item.business_id,
        "account_id": account.id,
        "provider_transaction_id": raw.provider_transaction_id,
        "amount": raw.amount,
        "date": raw.date,
        "vendor": raw.merchant_name,
        "description": raw.description,
        "pending": raw.pending,
        "provider_category": raw.provider_category,
        "raw_payload": raw.raw_payload,
    }
