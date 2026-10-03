"""Several businesses under one tenant stay separated."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from books.core import repository as repo
from books.core.accounting import AccountType
from books.core.categorization import apply_category
from books.core.errors import ValidationError
from books.core.sync import link_connection, sync_connection
from books.providers.fake import FakeState, default_state, make_transaction, reset_fake_state

pytestmark = pytest.mark.db


@pytest.fixture
async def two_businesses(session, business):
    """``business`` (Coding Crafts) plus a second one under the same tenant."""
    other = await repo.create_business(
        session, tenant_id=business.tenant_id, name="Side Studio", details="Indie product"
    )
    await session.commit()
    return business, other


async def _link_and_sync(session, business, token: str, state: FakeState):
    reset_fake_state(state)
    connection = await link_connection(
        session, business=business, provider_name="fake", public_token=token
    )
    summary = await sync_connection(session, connection=connection)
    await session.commit()
    return connection, summary


def _state_for(vendor: str, amount: str) -> FakeState:
    state = default_state(date(2026, 3, 1))
    state.pages[0] = type(state.pages[0])(
        added=[
            make_transaction(
                provider_transaction_id=f"{vendor}-1",
                account_id="fake-acct-checking",
                amount=amount,
                tx_date=date(2026, 2, 20),
                merchant_name=vendor,
            )
        ],
        modified=[],
        removed=[],
        next_cursor=f"cursor-{vendor}",
        has_more=False,
    )
    return state


async def test_each_business_gets_its_own_item_and_accounts(session, two_businesses):
    crafts, studio = two_businesses
    crafts_connection, _ = await _link_and_sync(
        session, crafts, "pt-crafts", _state_for("AWS", "-100")
    )
    studio_connection, _ = await _link_and_sync(
        session, studio, "pt-studio", _state_for("Stripe", "500")
    )

    assert crafts_connection.business_id == crafts.id
    assert studio_connection.business_id == studio.id

    crafts_accounts = await repo.list_accounts(session, business_id=crafts.id)
    studio_accounts = await repo.list_accounts(session, business_id=studio.id)
    assert len(crafts_accounts) == len(studio_accounts) == 1
    assert crafts_accounts[0].id != studio_accounts[0].id


async def test_transactions_do_not_leak_between_businesses(session, two_businesses):
    crafts, studio = two_businesses
    await _link_and_sync(session, crafts, "pt-crafts", _state_for("AWS", "-100"))
    await _link_and_sync(session, studio, "pt-studio", _state_for("Stripe", "500"))

    crafts_rows = await repo.list_transactions(
        session, repo.TransactionFilters(business_id=crafts.id, limit=100)
    )
    studio_rows = await repo.list_transactions(
        session, repo.TransactionFilters(business_id=studio.id, limit=100)
    )

    assert [t.vendor for t in crafts_rows] == ["AWS"]
    assert [t.vendor for t in studio_rows] == ["Stripe"]
    assert {t.id for t in crafts_rows}.isdisjoint({t.id for t in studio_rows})


async def test_the_same_category_name_can_exist_in_both(session, two_businesses):
    crafts, studio = two_businesses
    a = await repo.create_category(
        session, business=crafts, name="Travel", account_type=AccountType.EXPENSE
    )
    b = await repo.create_category(
        session, business=studio, name="Travel", account_type=AccountType.EXPENSE
    )
    assert a.id != b.id
    assert [c.name for c in await repo.list_categories(session, business_id=crafts.id)] == [
        "Travel"
    ]


async def test_a_category_cannot_be_applied_across_businesses(session, two_businesses):
    crafts, studio = two_businesses
    await _link_and_sync(session, crafts, "pt-crafts", _state_for("AWS", "-100"))
    studio_category = await repo.create_category(
        session, business=studio, name="Hosting", account_type=AccountType.EXPENSE
    )
    transaction = (
        await repo.list_transactions(session, repo.TransactionFilters(business_id=crafts.id))
    )[0]

    with pytest.raises(ValidationError, match="different business"):
        await apply_category(
            session,
            transaction=transaction,
            category_id=studio_category.id,
            actor="cli:hakeem",
            confidence=1.0,
        )


async def test_rules_only_apply_within_their_business(session, two_businesses):
    crafts, studio = two_businesses
    crafts_category = await repo.create_category(
        session, business=crafts, name="Cloud", account_type=AccountType.EXPENSE
    )
    await repo.create_rule(
        session,
        business=crafts,
        match_type="vendor_contains",
        pattern="AWS",
        category_id=crafts_category.id,
    )

    assert len(await repo.list_rules(session, business_id=crafts.id)) == 1
    assert await repo.list_rules(session, business_id=studio.id) == []


async def test_precedent_lookup_does_not_cross_businesses(session, two_businesses):
    """One business's human decisions must not steer another's categorization."""
    crafts, studio = two_businesses
    await _link_and_sync(session, crafts, "pt-crafts", _state_for("AWS", "-100"))
    await _link_and_sync(session, studio, "pt-studio", _state_for("AWS", "-100"))

    crafts_category = await repo.create_category(
        session, business=crafts, name="Cloud", account_type=AccountType.EXPENSE
    )
    crafts_tx = (
        await repo.list_transactions(session, repo.TransactionFilters(business_id=crafts.id))
    )[0]
    await apply_category(
        session,
        transaction=crafts_tx,
        category_id=crafts_category.id,
        actor="cli:hakeem",
        confidence=1.0,
        mark_reviewed=True,
    )

    studio_tx = (
        await repo.list_transactions(session, repo.TransactionFilters(business_id=studio.id))
    )[0]
    similar = await repo.find_similar_transactions(session, transaction=studio_tx, limit=10)
    assert similar == [], "Coding Crafts' AWS decision leaked into Side Studio"


async def test_seeding_each_business_is_independent(session, two_businesses):
    crafts, studio = two_businesses
    created, _ = await repo.seed_chart_of_accounts(session, business=crafts)
    assert created

    # The second business starts empty and seeds its own copy.
    assert await repo.list_categories(session, business_id=studio.id) == []
    studio_created, skipped = await repo.seed_chart_of_accounts(session, business=studio)
    assert len(studio_created) == len(created)
    assert skipped == []


async def test_syncing_all_items_covers_every_business(session, two_businesses):
    crafts, studio = two_businesses
    await _link_and_sync(session, crafts, "pt-crafts", _state_for("AWS", "-100"))
    await _link_and_sync(session, studio, "pt-studio", _state_for("Stripe", "500"))

    connections = await repo.list_connections(session, active_only=True)
    assert {i.business_id for i in connections} == {crafts.id, studio.id}


async def test_one_bank_connection_cannot_serve_two_businesses(session, two_businesses):
    """Re-linking the same institution elsewhere would double-count its money."""
    crafts, studio = two_businesses
    reset_fake_state(default_state(date(2026, 3, 1)))
    await link_connection(session, business=crafts, provider_name="fake", public_token="shared")

    with pytest.raises(ValidationError, match="already linked"):
        await link_connection(session, business=studio, provider_name="fake", public_token="shared")


async def test_amounts_stay_distinct_per_business(session, two_businesses):
    crafts, studio = two_businesses
    await _link_and_sync(session, crafts, "pt-crafts", _state_for("AWS", "-100"))
    await _link_and_sync(session, studio, "pt-studio", _state_for("Stripe", "500"))

    crafts_row = (
        await repo.list_transactions(session, repo.TransactionFilters(business_id=crafts.id))
    )[0]
    studio_row = (
        await repo.list_transactions(session, repo.TransactionFilters(business_id=studio.id))
    )[0]
    assert crafts_row.amount == Decimal("-100")
    assert crafts_row.entry_side == "debit"
    assert studio_row.amount == Decimal("500")
    assert studio_row.entry_side == "credit"
