"""The provider contract every aggregator adapter implements (plan.md §3.1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from books.core.accounting import AccountType

# Normalized webhook event types. Adapters map vendor-specific codes onto these
# so core code never branches on a vendor's vocabulary.
EVENT_SYNC_AVAILABLE = "sync_available"
EVENT_ITEM_ERROR = "item_error"
EVENT_ITEM_REVOKED = "item_revoked"
EVENT_UNKNOWN = "unknown"


@dataclass(frozen=True)
class LinkToken:
    token: str
    expiration: str


@dataclass(frozen=True)
class ItemCredentials:
    provider_item_id: str
    access_token: str  # encrypted before storage, never logged
    institution_name: str


@dataclass(frozen=True)
class RawAccount:
    provider_account_id: str
    name: str
    account_type: str | None = None  # the provider's own label, e.g. "depository/checking"
    classification: AccountType | None = None  # ASSET (money held) or LIABILITY (money owed)
    current_balance: Decimal | None = None


@dataclass(frozen=True)
class RawTransaction:
    provider_transaction_id: str
    account_id: str  # the provider's account id, not ours
    amount: Decimal  # signed, negative = money out
    date: date
    merchant_name: str | None
    description: str
    pending: bool
    provider_category: str | None
    raw_payload: dict[str, Any] = field(default_factory=dict)  # untouched original


@dataclass(frozen=True)
class SyncResult:
    added: list[RawTransaction]
    modified: list[RawTransaction]
    removed: list[str]  # provider_transaction_ids
    next_cursor: str | None
    has_more: bool


@dataclass(frozen=True)
class WebhookEvent:
    provider_item_id: str
    event_type: str  # one of the EVENT_* constants above
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class TransactionProvider(Protocol):
    """Implement all of this and the rest of the system works unchanged."""

    name: str

    def create_link_token(self, user_ref: str) -> LinkToken: ...

    def exchange_public_token(self, public_token: str) -> ItemCredentials: ...

    def sync_transactions(self, access_token: str, cursor: str | None) -> SyncResult: ...

    def get_accounts(self, access_token: str) -> list[RawAccount]: ...

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool: ...

    def parse_webhook(self, body: dict[str, Any]) -> WebhookEvent: ...

    def force_refresh(self, access_token: str) -> None: ...
