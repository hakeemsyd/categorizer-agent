"""Celery tasks: sync and categorize.

Both are thin — they open a session, call the core, and enqueue follow-up work.
All of the logic they invoke is equally reachable from the API and the CLI.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from celery import shared_task

from books.agent.categorizer import categorize_transaction as run_categorizer
from books.core import repository as repo
from books.core.db import session_scope
from books.core.errors import BooksError, ProviderError
from books.core.sync import sync_item as run_sync
from books.logging import get_logger
from books.workers.app import celery_app  # noqa: F401  (ensures app is configured)
from books.workers.runner import run_async

log = get_logger(__name__)


@shared_task(
    name="books.sync_item",
    bind=True,
    autoretry_for=(ProviderError,),
    retry_backoff=30,
    retry_backoff_max=900,
    retry_jitter=True,
    max_retries=5,
)
def sync_item(
    self, item_id: str, backfill: bool = False, since: str | None = None
) -> dict[str, Any]:
    """Drain one item's provider feed, then queue categorization for new rows."""
    summary = run_async(
        _sync(uuid.UUID(item_id), backfill, date.fromisoformat(since) if since else None)
    )
    for transaction_id in summary["new_transaction_ids"]:
        categorize_transaction.delay(transaction_id)
    return summary


async def _sync(item_id: uuid.UUID, backfill: bool, since: date | None) -> dict[str, Any]:
    async with session_scope() as session:
        item = await repo.get_item(session, item_id)
        summary = await run_sync(session, item=item, backfill=backfill, since=since)
        return {
            "item_id": str(summary.item_id),
            "pages": summary.pages,
            "inserted": summary.inserted,
            "updated": summary.updated,
            "removed": summary.removed,
            "skipped_before_backfill": summary.skipped_before_backfill,
            "new_transaction_ids": [str(i) for i in summary.new_transaction_ids],
        }


@shared_task(
    name="books.categorize_transaction",
    bind=True,
    retry_backoff=10,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=3,
)
def categorize_transaction(self, transaction_id: str) -> dict[str, Any]:
    try:
        return run_async(_categorize(uuid.UUID(transaction_id)))
    except BooksError:
        raise
    except Exception as exc:  # transient model/network failure
        raise self.retry(exc=exc) from exc


async def _categorize(transaction_id: uuid.UUID) -> dict[str, Any]:
    async with session_scope() as session:
        outcome = await run_categorizer(session, transaction_id=transaction_id)
        return {
            "transaction_id": str(outcome.transaction_id),
            "category_id": str(outcome.category_id) if outcome.category_id else None,
            "category_name": outcome.category_name,
            "account_type": outcome.account_type,
            "confidence": outcome.confidence,
            "needs_review": outcome.needs_review,
            "source": outcome.source,
        }


@shared_task(name="books.sync_all_items")
def sync_all_items() -> dict[str, int]:
    item_ids = run_async(_active_item_ids())
    for item_id in item_ids:
        sync_item.delay(item_id)
    log.info("sync_all_items.queued", count=len(item_ids))
    return {"queued": len(item_ids)}


async def _active_item_ids() -> list[str]:
    async with session_scope() as session:
        return [str(item.id) for item in await repo.list_items(session, active_only=True)]


@shared_task(name="books.recategorize_business")
def recategorize_business(business_id: str, only_uncategorized: bool = True) -> dict[str, int]:
    """Re-run the agent over a business — after editing the chart of accounts."""
    transaction_ids = run_async(
        _transactions_to_recategorize(uuid.UUID(business_id), only_uncategorized)
    )
    for transaction_id in transaction_ids:
        categorize_transaction.delay(transaction_id)
    return {"queued": len(transaction_ids)}


async def _transactions_to_recategorize(
    business_id: uuid.UUID, only_uncategorized: bool
) -> list[str]:
    async with session_scope() as session:
        filters = repo.TransactionFilters(
            business_id=business_id,
            uncategorized=only_uncategorized or None,
            limit=10_000,
        )
        return [str(t.id) for t in await repo.list_transactions(session, filters)]
