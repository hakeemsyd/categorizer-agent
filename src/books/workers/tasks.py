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
from books.core.sync import sync_connection as run_sync
from books.logging import get_logger
from books.workers.app import celery_app  # noqa: F401  (ensures app is configured)
from books.workers.runner import run_async

log = get_logger(__name__)


@shared_task(
    name="books.sync_connection",
    bind=True,
    autoretry_for=(ProviderError,),
    retry_backoff=30,
    retry_backoff_max=900,
    retry_jitter=True,
    max_retries=5,
)
def sync_connection(
    self, connection_id: str, backfill: bool = False, since: str | None = None
) -> dict[str, Any]:
    """Drain one connection's provider feed, then queue categorization for new rows."""
    summary = run_async(
        _sync(uuid.UUID(connection_id), backfill, date.fromisoformat(since) if since else None)
    )
    for transaction_id in summary["new_transaction_ids"]:
        categorize_transaction.delay(transaction_id)
    return summary


async def _sync(connection_id: uuid.UUID, backfill: bool, since: date | None) -> dict[str, Any]:
    async with session_scope() as session:
        connection = await repo.get_connection(session, connection_id)
        summary = await run_sync(session, connection=connection, backfill=backfill, since=since)
        return {
            "connection_id": str(summary.connection_id),
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
def categorize_transaction(self, transaction_id: str, force: bool = False) -> dict[str, Any]:
    try:
        return run_async(_categorize(uuid.UUID(transaction_id), force=force))
    except BooksError:
        # Includes a refusal to overwrite a human-reviewed transaction
        # (ValidationError) — a real outcome, not worth retrying.
        raise
    except Exception as exc:  # transient model/network failure
        raise self.retry(exc=exc) from exc


async def _categorize(transaction_id: uuid.UUID, force: bool = False) -> dict[str, Any]:
    async with session_scope() as session:
        outcome = await run_categorizer(session, transaction_id=transaction_id, force=force)
        return {
            "transaction_id": str(outcome.transaction_id),
            "category_id": str(outcome.category_id) if outcome.category_id else None,
            "category_name": outcome.category_name,
            "account_type": outcome.account_type,
            "confidence": outcome.confidence,
            "needs_review": outcome.needs_review,
            "source": outcome.source,
        }


@shared_task(name="books.sync_all_connections")
def sync_all_connections() -> dict[str, int]:
    connection_ids = run_async(_active_connection_ids())
    for connection_id in connection_ids:
        sync_connection.delay(connection_id)
    log.info("sync_all_connections.queued", count=len(connection_ids))
    return {"queued": len(connection_ids)}


async def _active_connection_ids() -> list[str]:
    async with session_scope() as session:
        return [
            str(connection.id)
            for connection in await repo.list_connections(session, active_only=True)
        ]
