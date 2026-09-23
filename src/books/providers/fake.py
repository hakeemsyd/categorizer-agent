"""In-memory provider used by tests and local demos.

Exists to prove the abstraction is real (plan.md §3.3): every route, Celery
task, and the categorization agent are exercised against this, so any
provider-specific detail that leaks out of a real adapter breaks a test.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from books.core.accounting import AccountType
from books.core.errors import WebhookVerificationError
from books.providers.base import (
    EVENT_SYNC_AVAILABLE,
    EVENT_UNKNOWN,
    ItemCredentials,
    LinkToken,
    RawAccount,
    RawTransaction,
    SyncResult,
    WebhookEvent,
)

# Shared across FakeProvider instances so the registry can hand out a fresh
# object per call while tests keep seeding one canned dataset.
_STATE: FakeState | None = None


@dataclass
class FakeState:
    accounts: list[RawAccount] = field(default_factory=list)
    pages: list[SyncResult] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    institution_name: str = "Fake Federal Credit Union"
    verify_ok: bool = True
    refresh_calls: list[str] = field(default_factory=list)
    _ids = itertools.count(1)


def reset_fake_state(state: FakeState | None = None) -> FakeState:
    global _STATE
    _STATE = state or FakeState()
    return _STATE


def fake_state() -> FakeState:
    global _STATE
    if _STATE is None:
        _STATE = default_state()
    return _STATE


def make_transaction(
    *,
    provider_transaction_id: str,
    account_id: str,
    amount: str | Decimal,
    tx_date: date,
    merchant_name: str | None = None,
    description: str = "",
    pending: bool = False,
    provider_category: str | None = None,
) -> RawTransaction:
    return RawTransaction(
        provider_transaction_id=provider_transaction_id,
        account_id=account_id,
        amount=Decimal(str(amount)),
        date=tx_date,
        merchant_name=merchant_name,
        description=description or (merchant_name or ""),
        pending=pending,
        provider_category=provider_category,
        raw_payload={"source": "fake", "transaction_id": provider_transaction_id},
    )


def default_state(today: date | None = None) -> FakeState:
    """A small canned dataset: one checking account, a handful of spend."""
    today = today or date.today()
    account_id = "fake-acct-checking"
    state = FakeState(
        accounts=[
            RawAccount(
                provider_account_id=account_id,
                name="Business Checking",
                account_type="depository/checking",
                classification=AccountType.ASSET,
                current_balance=Decimal("18422.31"),
            )
        ]
    )
    rows = [
        ("AWS", "-412.55", "AMAZON WEB SERVICES AWS.AMAZON.CO", "CLOUD_COMPUTING", 1),
        ("Gusto", "-9840.00", "GUSTO PAYROLL", "PAYROLL", 3),
        ("Acme Corp", "25000.00", "ACME CORP ACH CREDIT INV-1042", "INCOME", 5),
        ("Delta", "-628.40", "DELTA AIR LINES TICKET", "TRAVEL", 8),
        ("Notion", "-48.00", "NOTION LABS SUBSCRIPTION", "SOFTWARE", 11),
    ]
    state.pages = [
        SyncResult(
            added=[
                make_transaction(
                    provider_transaction_id=f"fake-tx-{i}",
                    account_id=account_id,
                    amount=amount,
                    tx_date=today - timedelta(days=days_ago),
                    merchant_name=merchant,
                    description=description,
                    provider_category=category,
                )
                for i, (merchant, amount, description, category, days_ago) in enumerate(rows, 1)
            ],
            modified=[],
            removed=[],
            next_cursor="fake-cursor-1",
            has_more=False,
        )
    ]
    return state


class FakeProvider:
    """Implements :class:`books.providers.base.TransactionProvider`."""

    name = "fake"

    def __init__(self, state: FakeState | None = None) -> None:
        self.state = state or fake_state()

    def create_link_token(self, user_ref: str) -> LinkToken:
        return LinkToken(token=f"fake-link-token-{user_ref}", expiration="2099-01-01T00:00:00Z")

    def exchange_public_token(self, public_token: str) -> ItemCredentials:
        return ItemCredentials(
            provider_item_id=f"fake-item-{public_token}",
            access_token=f"fake-access-{public_token}",
            institution_name=self.state.institution_name,
        )

    def sync_transactions(self, access_token: str, cursor: str | None) -> SyncResult:
        pages = self.state.pages
        if not pages:
            return SyncResult([], [], [], cursor, False)

        index = (
            0
            if cursor is None
            else next(
                (i + 1 for i, page in enumerate(pages) if page.next_cursor == cursor), len(pages)
            )
        )
        if index >= len(pages):
            return SyncResult([], [], [], cursor, False)
        return pages[index]

    def get_accounts(self, access_token: str) -> list[RawAccount]:
        return list(self.state.accounts)

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        if not self.state.verify_ok:
            raise WebhookVerificationError("FakeProvider configured to reject webhooks")
        return True

    def parse_webhook(self, body: dict[str, Any]) -> WebhookEvent:
        return WebhookEvent(
            provider_item_id=str(body.get("item_id", "")),
            event_type=str(body.get("event_type") or EVENT_SYNC_AVAILABLE) or EVENT_UNKNOWN,
            raw=body,
        )

    def force_refresh(self, access_token: str) -> None:
        self.state.refresh_calls.append(access_token)
