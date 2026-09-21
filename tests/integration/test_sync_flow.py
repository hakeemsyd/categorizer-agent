"""End-to-end sync against FakeProvider — no vendor SDK, no network."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from books.core import repository as repo
from books.core.errors import ValidationError
from books.core.sync import link_item, sync_item
from books.providers.base import SyncResult
from books.providers.fake import make_transaction

pytestmark = pytest.mark.db


async def test_link_stores_an_encrypted_token_and_seeds_accounts(session, business, fake_state):
    item = await link_item(session, business=business, provider_name="fake", public_token="pt-1")

    assert item.access_token_encrypted and "fake-access" not in item.access_token_encrypted
    from books.security import crypto

    assert crypto.decrypt(item.access_token_encrypted) == "fake-access-pt-1"

    accounts = await repo.list_accounts(session, business_id=business.id)
    assert [a.name for a in accounts] == ["Business Checking"]
    assert accounts[0].classification == "asset"


async def test_linking_the_same_institution_twice_is_refused(session, business, fake_state):
    await link_item(session, business=business, provider_name="fake", public_token="pt-1")
    with pytest.raises(ValidationError, match="already linked"):
        await link_item(session, business=business, provider_name="fake", public_token="pt-1")


async def test_sync_ingests_and_advances_the_cursor(session, linked_item):
    summary = await sync_item(session, item=linked_item)

    assert summary.inserted == 5
    assert summary.new_count == 5
    assert linked_item.cursor == "fake-cursor-1"
    assert linked_item.last_synced_at is not None

    transactions = await repo.list_transactions(
        session, repo.TransactionFilters(business_id=linked_item.business_id, limit=100)
    )
    by_vendor = {t.vendor: t for t in transactions}
    assert by_vendor["AWS"].amount == Decimal("-412.55")
    assert by_vendor["AWS"].transaction_type == "debit"
    assert by_vendor["Acme Corp"].amount == Decimal("25000.00")
    assert by_vendor["Acme Corp"].transaction_type == "credit"
    # Every ingested row starts uncategorized and untouched by a human.
    assert all(t.category_id is None and t.last_reviewed_at is None for t in transactions)


async def test_resync_is_idempotent(session, linked_item):
    await sync_item(session, item=linked_item)
    second = await sync_item(session, item=linked_item, backfill=True)

    assert second.inserted == 0
    assert second.updated == 5
    assert (
        await repo.count_transactions(
            session, repo.TransactionFilters(business_id=linked_item.business_id)
        )
        == 5
    )


async def test_backfill_since_discards_older_rows_at_ingestion(session, linked_item):
    """plan.md §5: the provider has no date range, so the boundary is ours."""
    # Canned rows are 1, 3, 5, 8 and 11 days before 2026-03-01.
    summary = await sync_item(session, item=linked_item, backfill=True, since=date(2026, 2, 25))

    assert summary.inserted == 2  # only 02-28 and 02-26 survive the boundary
    assert summary.skipped_before_backfill == 3
    assert linked_item.backfill_start_date == date(2026, 2, 25)


async def test_ongoing_sync_is_never_date_bounded(session, linked_item):
    """A stored backfill boundary must not silently filter ongoing syncs."""
    linked_item.backfill_start_date = date(2026, 2, 25)
    summary = await sync_item(session, item=linked_item, backfill=False)
    assert summary.inserted == 5
    assert summary.skipped_before_backfill == 0


async def test_removed_transactions_are_deleted(session, linked_item, fake_state):
    await sync_item(session, item=linked_item)

    # The provider reports a deletion on the next page of the same feed.
    fake_state.pages.append(
        SyncResult(
            added=[],
            modified=[],
            removed=["fake-tx-1"],
            next_cursor="fake-cursor-2",
            has_more=False,
        )
    )

    summary = await sync_item(session, item=linked_item)
    assert summary.removed == 1
    assert (
        await repo.count_transactions(
            session, repo.TransactionFilters(business_id=linked_item.business_id)
        )
        == 4
    )


async def test_multi_page_sync_drains_every_page(session, linked_item, fake_state):
    account_id = "fake-acct-checking"
    fake_state.pages = [
        SyncResult(
            added=[
                make_transaction(
                    provider_transaction_id=f"p1-{i}",
                    account_id=account_id,
                    amount="-5.00",
                    tx_date=date(2026, 2, 1),
                    merchant_name=f"Vendor {i}",
                )
                for i in range(3)
            ],
            modified=[],
            removed=[],
            next_cursor="page-1",
            has_more=True,
        ),
        SyncResult(
            added=[
                make_transaction(
                    provider_transaction_id="p2-1",
                    account_id=account_id,
                    amount="-6.00",
                    tx_date=date(2026, 2, 2),
                    merchant_name="Vendor last",
                )
            ],
            modified=[],
            removed=[],
            next_cursor="page-2",
            has_more=False,
        ),
    ]

    summary = await sync_item(session, item=linked_item, backfill=True)
    assert summary.pages == 2
    assert summary.inserted == 4
    assert linked_item.cursor == "page-2"
