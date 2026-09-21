"""The provider abstraction has to be real, not aspirational (plan.md §3.3)."""

from __future__ import annotations

import inspect
from datetime import date
from decimal import Decimal

import pytest

from books.core.errors import ConfigurationError
from books.providers import get_provider
from books.providers.base import TransactionProvider
from books.providers.fake import FakeProvider
from books.providers.registry import available_providers

PROTOCOL_METHODS = [
    "create_link_token",
    "exchange_public_token",
    "sync_transactions",
    "get_accounts",
    "verify_webhook",
    "parse_webhook",
    "force_refresh",
]


@pytest.mark.parametrize("provider_name", ["plaid", "fake"])
def test_every_registered_provider_implements_the_protocol(provider_name: str) -> None:
    provider = get_provider(provider_name)
    assert isinstance(provider, TransactionProvider)
    for method in PROTOCOL_METHODS:
        assert callable(getattr(provider, method)), f"{provider_name} is missing {method}"


@pytest.mark.parametrize("provider_name", ["plaid", "fake"])
def test_signatures_match_the_protocol(provider_name: str) -> None:
    provider = get_provider(provider_name)
    for method in PROTOCOL_METHODS:
        expected = inspect.signature(getattr(TransactionProvider, method))
        actual = inspect.signature(getattr(provider, method))
        assert list(actual.parameters) == [p for p in expected.parameters if p != "self"], (
            f"{provider_name}.{method} signature drifted from the protocol"
        )


def test_unknown_provider_is_a_configuration_error() -> None:
    with pytest.raises(ConfigurationError, match="Unknown provider"):
        get_provider("definitely-not-a-bank")


def test_registry_lists_what_is_available() -> None:
    assert {"plaid", "fake"} <= set(available_providers())


def test_fake_provider_paginates_by_cursor() -> None:
    from books.providers.base import SyncResult
    from books.providers.fake import FakeState, make_transaction

    page_one = SyncResult(
        added=[
            make_transaction(
                provider_transaction_id="a",
                account_id="acct",
                amount="-10.00",
                tx_date=date(2026, 1, 1),
            )
        ],
        modified=[],
        removed=[],
        next_cursor="c1",
        has_more=True,
    )
    page_two = SyncResult(added=[], modified=[], removed=["a"], next_cursor="c2", has_more=False)
    provider = FakeProvider(FakeState(pages=[page_one, page_two]))

    first = provider.sync_transactions("token", None)
    assert first.has_more and first.next_cursor == "c1"
    second = provider.sync_transactions("token", "c1")
    assert second.removed == ["a"] and not second.has_more
    # Draining past the end is a no-op, not an error.
    assert provider.sync_transactions("token", "c2").added == []


def test_plaid_normalizes_amount_sign_and_shape() -> None:
    """Plaid reports money out as positive; we store signed amounts."""
    from books.providers.plaid import PlaidProvider

    raw = PlaidProvider._to_transaction(
        {
            "transaction_id": "tx-1",
            "account_id": "acct-1",
            "amount": 412.55,  # Plaid: money leaving the account
            "date": "2026-02-14",
            "merchant_name": "Amazon Web Services",
            "name": "AWS",
            "original_description": "AMAZON WEB SERVICES AWS.AMAZON.CO",
            "pending": False,
            "personal_finance_category": {"primary": "GENERAL_SERVICES", "detailed": "CLOUD"},
        }
    )
    assert raw.amount == Decimal("-412.55")
    assert raw.date == date(2026, 2, 14)
    assert raw.merchant_name == "Amazon Web Services"
    assert raw.provider_category == "CLOUD"
    assert raw.raw_payload["transaction_id"] == "tx-1"


def test_plaid_webhook_codes_map_to_normalized_events() -> None:
    from books.providers.base import EVENT_ITEM_REVOKED, EVENT_SYNC_AVAILABLE, EVENT_UNKNOWN
    from books.providers.plaid import PlaidProvider

    provider = PlaidProvider()
    assert (
        provider.parse_webhook(
            {
                "webhook_type": "TRANSACTIONS",
                "webhook_code": "SYNC_UPDATES_AVAILABLE",
                "item_id": "i",
            }
        ).event_type
        == EVENT_SYNC_AVAILABLE
    )
    assert (
        provider.parse_webhook(
            {"webhook_type": "ITEM", "webhook_code": "USER_PERMISSION_REVOKED", "item_id": "i"}
        ).event_type
        == EVENT_ITEM_REVOKED
    )
    assert (
        provider.parse_webhook(
            {"webhook_type": "ASSETS", "webhook_code": "PRODUCT_READY"}
        ).event_type
        == EVENT_UNKNOWN
    )
