"""The provider contract every aggregator adapter implements (plan.md §3.1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from books.core.accounting import AccountType

# Normalized webhook event types. Adapters map vendor-specific codes onto these
# so core code never branches on a vendor's vocabulary.
EVENT_SYNC_AVAILABLE = "sync_available"
EVENT_ITEM_ERROR = "connection_error"
EVENT_ITEM_REVOKED = "connection_revoked"
EVENT_UNKNOWN = "unknown"


@dataclass(frozen=True)
class LinkToken:
    token: str
    expiration: str


@dataclass(frozen=True)
class TokenSet:
    """What an OAuth provider hands back, and what must be stored.

    ``refresh_token`` is not optional bookkeeping for providers that rotate it:
    Fintable issues a new one on every refresh and invalidates the old, so
    losing a rotation costs the connection outright — the only recovery is
    sending a human back through the browser. Anything holding one of these
    must persist it before using the access token beside it.
    """

    access_token: str  # encrypted before storage, never logged
    refresh_token: str | None = None
    expires_at: datetime | None = None

    @property
    def expires_soon(self) -> bool:
        """True with a minute of headroom, so a sync does not die mid-page."""
        if self.expires_at is None:
            return False
        return self.expires_at <= datetime.now(tz=UTC) + timedelta(seconds=60)


@dataclass(frozen=True)
class ItemCredentials:
    provider_ref: str
    access_token: str  # encrypted before storage, never logged
    institution_name: str
    refresh_token: str | None = None
    expires_at: datetime | None = None

    @property
    def tokens(self) -> TokenSet:
        return TokenSet(self.access_token, self.refresh_token, self.expires_at)


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
    provider_ref: str
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


@runtime_checkable
class OAuthProvider(Protocol):
    """A provider whose access tokens expire and must be renewed.

    Separate from TransactionProvider because renewal is not every provider's
    problem, and because the *core* drives it: core.sync refreshes, persists
    and commits before it calls anything here with the result. Providers stay
    stateless HTTP adapters that never reach for the database.
    """

    def refresh_tokens(self, refresh_token: str) -> TokenSet: ...
