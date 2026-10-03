"""Accounting semantics as the database enforces them."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from books.core import repository as repo
from books.core.accounting import AccountType, EntrySide, entry_side_for_amount
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
    session, business, linked_connection, amount, expected
):
    transaction = await _tx(session, business, amount, vendor=f"v{amount}")
    assert transaction.entry_side == expected


async def test_entry_side_cannot_be_written_by_hand(session, business, linked_connection):
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


async def test_entry_side_follows_a_corrected_amount(session, business, linked_connection):
    """Correct the amount and the side re-derives — it cannot be left stale."""
    transaction = await _tx(session, business, "-100.00", vendor="flip")
    assert transaction.entry_side == EntrySide.DEBIT

    transaction.amount = Decimal("100.00")
    await session.flush()
    await session.refresh(transaction)
    assert transaction.entry_side == EntrySide.CREDIT


async def test_a_card_purchase_and_a_debit_card_purchase_agree(
    session, business, linked_connection
):
    """One convention, whatever account the money left from.

    This test used to assert the opposite — that a positive amount on a credit
    card meant money out — because Teller signed from the account's point of
    view. Fintable normalizes before we see it, so the adapter owns that and
    the ledger does not second-guess the sign.
    """
    card = await repo.upsert_account(
        session,
        connection=await repo.get_connection(session, linked_connection.id),
        raw=RawAccount(
            provider_account_id="card-1",
            name="Platinum Card",
            account_type="credit / credit_card",
            classification=AccountType.LIABILITY,
        ),
    )
    await session.flush()

    on_card = await _tx(session, business, "-123.46", vendor="IKEA", account=card)
    refund = await _tx(session, business, "123.46", vendor="IKEAREFUND", account=card)

    assert on_card.entry_side == EntrySide.DEBIT  # money out
    assert refund.entry_side == EntrySide.CREDIT  # money back in


async def test_the_database_agrees_with_the_python_rule(session, business, linked_connection):
    """Two statements of one rule, so this pins them together.

    accounting.entry_side_for_amount is what the code reasons with; the trigger
    in models.ENTRY_SIDE_FUNCTION is what the data obeys. If they ever drift,
    every downstream decision quietly splits in two.
    """
    card = await repo.upsert_account(
        session,
        connection=await repo.get_connection(session, linked_connection.id),
        raw=RawAccount(
            provider_account_id="card-2",
            name="Card",
            account_type="credit / credit_card",
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
            assert row.entry_side == entry_side_for_amount(Decimal(amount)), (
                f"{account.classification} {amount}"
            )
