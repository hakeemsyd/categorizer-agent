"""Accounting semantics as the database enforces them."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from books.core import repository as repo
from books.core.accounting import AccountType, EntrySide, entry_side_for_amount
from books.core.chart_of_accounts import DEFAULT_CHART
from books.core.errors import ValidationError
from books.core.sync import sync_item
from books.providers.base import RawAccount

pytestmark = pytest.mark.db


async def _tx(session, business, amount: str, vendor: str = "V", account=None):
    account = account or (await repo.list_accounts(session, business_id=business.id))[0]
    transaction_id, _ = await repo.upsert_transaction(
        session,
        values={
            "tenant_id": business.tenant_id,
            "business_id": business.id,
            "account_id": account.id,
            "provider_transaction_id": f"{vendor}-{amount}",
            "amount": Decimal(amount),
            "date": date(2026, 2, 14),
            "vendor": vendor,
        },
    )
    await session.flush()
    return await repo.get_transaction(session, transaction_id)


# --- entry_side, derived by the database ---------------------------------


@pytest.mark.parametrize(
    ("amount", "expected"),
    [("-412.55", "debit"), ("-0.01", "debit"), ("0.00", "credit"), ("25000.00", "credit")],
)
async def test_entry_side_is_generated_from_the_amount(
    session, business, linked_item, amount, expected
):
    transaction = await _tx(session, business, amount, vendor=f"v{amount}")
    assert transaction.entry_side == expected


async def test_entry_side_cannot_be_written_by_hand(session, business, linked_item):
    """A trigger owns the column, so no statement can put it out of sync.

    Raw SQL that names a side is not rejected — it is overruled, which is the
    stronger outcome: a writer that gets it wrong cannot leave wrong data.
    """
    account = (await repo.list_accounts(session, business_id=business.id))[0]
    await session.execute(
        text(
            "INSERT INTO transactions "
            "(tenant_id, business_id, account_id, provider_transaction_id, "
            " amount, date, entry_side) "
            "VALUES (:t, :b, :a, 'forced', -5, '2026-01-01', 'credit')"
        ),
        {"t": business.tenant_id, "b": business.id, "a": account.id},
    )
    side = await session.scalar(
        text("SELECT entry_side FROM transactions WHERE provider_transaction_id = 'forced'")
    )
    assert side == "debit"
    await session.rollback()


async def test_entry_side_follows_a_corrected_amount(session, business, linked_item):
    """Correct the amount and the side re-derives — it cannot be left stale."""
    transaction = await _tx(session, business, "-100.00", vendor="flip")
    assert transaction.entry_side == EntrySide.DEBIT

    transaction.amount = Decimal("100.00")
    await session.flush()
    await session.refresh(transaction)
    assert transaction.entry_side == EntrySide.CREDIT


async def test_a_credit_card_purchase_is_money_out_though_the_amount_is_positive(
    session, business, linked_item
):
    """The bug this whole derivation exists for.

    Teller signs amounts from the account's point of view, and a credit card
    is a liability: a purchase increases what you owe, so it arrives positive.
    Reading the sign alone booked 101 of 110 rows on one real card as income.
    """
    card = await repo.upsert_account(
        session,
        item=await repo.get_item(session, linked_item.id),
        raw=RawAccount(
            provider_account_id="card-1",
            name="Platinum Card",
            account_type="credit/credit_card",
            classification=AccountType.LIABILITY,
        ),
    )
    await session.flush()

    purchase = await _tx(session, business, "123.46", vendor="IKEA", account=card)
    payment = await _tx(session, business, "-500.00", vendor="PAYMENT", account=card)

    assert purchase.entry_side == EntrySide.DEBIT  # money out: a charge
    assert payment.entry_side == EntrySide.CREDIT  # money in: paying it down


async def test_the_database_agrees_with_the_python_rule(session, business, linked_item):
    """Two statements of one rule, so this pins them together.

    accounting.entry_side_for_amount is what the code reasons with; the trigger
    in models.ENTRY_SIDE_FUNCTION is what the data obeys. If they ever drift,
    every downstream decision quietly splits in two.
    """
    card = await repo.upsert_account(
        session,
        item=await repo.get_item(session, linked_item.id),
        raw=RawAccount(
            provider_account_id="card-2",
            name="Card",
            account_type="credit/credit_card",
            classification=AccountType.LIABILITY,
        ),
    )
    await session.flush()
    checking = (await repo.list_accounts(session, business_id=business.id))[0]

    for account in (checking, card):
        for amount in ("-412.55", "-0.01", "0.00", "0.01", "25000.00"):
            row = await _tx(
                session, business, amount, vendor=f"{account.name}{amount}", account=account
            )
            assert row.entry_side == entry_side_for_amount(
                Decimal(amount), account.classification
            ), f"{account.classification} {amount}"


async def test_synced_transactions_all_carry_an_entry_side(session, linked_item):
    await sync_item(session, item=linked_item)
    transactions = await repo.list_transactions(
        session, repo.TransactionFilters(business_id=linked_item.business_id, limit=100)
    )
    by_vendor = {t.vendor: t for t in transactions}
    assert by_vendor["AWS"].entry_side == EntrySide.DEBIT  # money out
    assert by_vendor["Acme Corp"].entry_side == EntrySide.CREDIT  # money in
    assert all(t.entry_side in (EntrySide.DEBIT, EntrySide.CREDIT) for t in transactions)


# --- categories carry an account type ------------------------------------


async def test_category_exposes_its_normal_balance(session, business):
    revenue = await repo.create_category(
        session, business=business, name="Sales", account_type=AccountType.REVENUE
    )
    expense = await repo.create_category(
        session, business=business, name="Rent", account_type="expense"
    )
    assert revenue.normal_balance is EntrySide.CREDIT
    assert expense.normal_balance is EntrySide.DEBIT


async def test_an_invalid_account_type_is_rejected_with_the_valid_list(session, business):
    with pytest.raises(ValidationError, match="is not an account type"):
        await repo.create_category(
            session, business=business, name="Nonsense", account_type="profit"
        )


async def test_a_sub_account_must_match_its_parent_type(session, business):
    parent = await repo.create_category(
        session, business=business, name="Operating Costs", account_type=AccountType.EXPENSE
    )
    child = await repo.create_category(
        session,
        business=business,
        name="Cloud",
        account_type=AccountType.EXPENSE,
        parent_category_id=parent.id,
    )
    assert child.parent_category_id == parent.id

    with pytest.raises(ValidationError, match="share its parent's account type"):
        await repo.create_category(
            session,
            business=business,
            name="Mislabelled",
            account_type=AccountType.REVENUE,
            parent_category_id=parent.id,
        )


async def test_bank_accounts_are_classified_asset_or_liability(session, linked_item):
    accounts = await repo.list_accounts(session, business_id=linked_item.business_id)
    assert accounts[0].classification is AccountType.ASSET


# --- seeding --------------------------------------------------------------


async def test_seeding_creates_the_whole_default_chart(session, business):
    created, skipped = await repo.seed_chart_of_accounts(session, business=business)
    assert len(created) == len(DEFAULT_CHART)
    assert skipped == []

    stored = await repo.list_categories(session, business_id=business.id)
    assert len(stored) == len(DEFAULT_CHART)
    # Listed grouped by account type, which is how a chart of accounts reads.
    types = [c.account_type for c in stored]
    assert types == sorted(types, key=lambda t: list(AccountType).index(t))


async def test_seeding_twice_is_idempotent(session, business):
    await repo.seed_chart_of_accounts(session, business=business)
    created, skipped = await repo.seed_chart_of_accounts(session, business=business)

    assert created == []
    assert len(skipped) == len(DEFAULT_CHART)


async def test_seeding_tops_up_a_partial_chart_without_touching_existing_rows(session, business):
    mine = await repo.create_category(
        session,
        business=business,
        name="Travel",
        account_type=AccountType.EXPENSE,
        description="My own wording",
    )
    created, skipped = await repo.seed_chart_of_accounts(session, business=business)

    assert "Travel" in skipped
    assert len(created) == len(DEFAULT_CHART) - 1
    assert mine.description == "My own wording"


async def test_categories_can_be_filtered_by_account_type(session, business):
    await repo.seed_chart_of_accounts(session, business=business)
    revenue = await repo.list_categories(
        session, business_id=business.id, account_type=AccountType.REVENUE
    )
    assert revenue
    assert {c.account_type for c in revenue} == {AccountType.REVENUE}
