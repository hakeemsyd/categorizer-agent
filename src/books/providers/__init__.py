"""Aggregator adapters.

Nothing outside this package imports a vendor SDK. Swapping Plaid for another
aggregator means adding one module here and registering it — routes, Celery
tasks, schema, and the categorization agent are untouched (plan.md §3).
"""

from books.providers.base import (
    ItemCredentials,
    LinkToken,
    RawTransaction,
    SyncResult,
    TransactionProvider,
    WebhookEvent,
)
from books.providers.registry import get_provider, register_provider

__all__ = [
    "ItemCredentials",
    "LinkToken",
    "RawTransaction",
    "SyncResult",
    "TransactionProvider",
    "WebhookEvent",
    "get_provider",
    "register_provider",
]
