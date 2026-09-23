"""Indirection between the core and Celery.

Routes and CLI never import ``books.workers.tasks`` directly: they call these
functions. Tests (and a future in-process mode) swap the dispatcher instead of
standing up Redis.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Protocol


class Dispatcher(Protocol):
    def sync_item(
        self, item_id: uuid.UUID, *, backfill: bool, since: date | None
    ) -> str | None: ...

    def categorize_transaction(
        self, transaction_id: uuid.UUID, *, force: bool = False
    ) -> str | None: ...


class CeleryDispatcher:
    """Default: hand the work to a Celery worker."""

    def sync_item(self, item_id: uuid.UUID, *, backfill: bool, since: date | None) -> str | None:
        from books.workers.tasks import sync_item

        result = sync_item.delay(str(item_id), backfill, since.isoformat() if since else None)
        return result.id

    def categorize_transaction(
        self, transaction_id: uuid.UUID, *, force: bool = False
    ) -> str | None:
        from books.workers.tasks import categorize_transaction

        return categorize_transaction.delay(str(transaction_id), force).id


class RecordingDispatcher:
    """Records calls instead of queueing. Used by tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def sync_item(self, item_id: uuid.UUID, *, backfill: bool, since: date | None) -> str | None:
        self.calls.append(("sync_item", (item_id, backfill, since)))
        return None

    def categorize_transaction(
        self, transaction_id: uuid.UUID, *, force: bool = False
    ) -> str | None:
        self.calls.append(("categorize_transaction", (transaction_id, force)))
        return None


_dispatcher: Dispatcher = CeleryDispatcher()


def get_dispatcher() -> Dispatcher:
    return _dispatcher


def set_dispatcher(dispatcher: Dispatcher) -> Dispatcher:
    global _dispatcher
    previous, _dispatcher = _dispatcher, dispatcher
    return previous
