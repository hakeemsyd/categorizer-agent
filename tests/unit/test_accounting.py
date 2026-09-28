"""The accounting rules themselves — normal balances and entry sides."""

from __future__ import annotations

from decimal import Decimal

import pytest

from books.core.accounting import (
    NORMAL_BALANCE,
    AccountType,
    EntrySide,
    entry_side_for_amount,
    increases_balance,
    normal_balance,
    signed_for_reporting,
)
from books.core.chart_of_accounts import DEFAULT_CHART, chart_summary


@pytest.mark.parametrize(
    ("account_type", "expected"),
    [
        (AccountType.ASSET, EntrySide.DEBIT),
        (AccountType.EXPENSE, EntrySide.DEBIT),
        (AccountType.LIABILITY, EntrySide.CREDIT),
        (AccountType.EQUITY, EntrySide.CREDIT),
        (AccountType.REVENUE, EntrySide.CREDIT),
    ],
)
def test_normal_balances_follow_the_accounting_equation(account_type, expected) -> None:
    assert normal_balance(account_type) is expected


def test_every_account_type_has_a_normal_balance() -> None:
    assert set(NORMAL_BALANCE) == set(AccountType)


def test_normal_balance_accepts_the_raw_string() -> None:
    assert normal_balance("revenue") is EntrySide.CREDIT


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("-412.55", EntrySide.DEBIT),  # money out
        ("-0.01", EntrySide.DEBIT),
        ("0", EntrySide.CREDIT),  # documented edge case
        ("0.01", EntrySide.CREDIT),
        ("25000.00", EntrySide.CREDIT),  # money in
    ],
)
def test_on_a_bank_account_the_sign_says_which_way_money_went(amount, expected) -> None:
    assert entry_side_for_amount(Decimal(amount), AccountType.ASSET) is expected


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        # A card purchase is POSITIVE: it increases what you owe. Reading the
        # sign alone booked all of these as income.
        ("123.46", EntrySide.DEBIT),
        ("0.01", EntrySide.DEBIT),
        # Paying the card down, or a refund onto it, reduces the balance owed.
        ("-500.00", EntrySide.CREDIT),
        ("0", EntrySide.CREDIT),
    ],
)
def test_on_a_credit_card_the_sign_is_inverted(amount, expected) -> None:
    """The bug this signature exists to prevent.

    Teller signs amounts from the account's point of view, and a credit card
    is a liability: spending grows it. 101 of 110 rows on one real card were
    positive, and every one of them was being treated as money in.
    """
    assert entry_side_for_amount(Decimal(amount), AccountType.LIABILITY) is expected


def test_an_unclassified_account_is_read_as_an_asset() -> None:
    """The safe assumption when a provider told us nothing about the account."""
    assert entry_side_for_amount(Decimal("-10"), None) is EntrySide.DEBIT
    assert entry_side_for_amount(Decimal("10"), None) is EntrySide.CREDIT


def test_the_same_purchase_debits_its_category_whichever_account_paid() -> None:
    """The point of the whole correction: one real-world event, one answer."""
    on_card = entry_side_for_amount(Decimal("123.46"), AccountType.LIABILITY)
    on_debit_card = entry_side_for_amount(Decimal("-123.46"), AccountType.ASSET)
    assert on_card is on_debit_card is EntrySide.DEBIT


def test_increases_balance_distinguishes_a_cost_from_a_refund() -> None:
    # Spending debits an expense — the expense grows.
    assert increases_balance(AccountType.EXPENSE, EntrySide.DEBIT) is True
    # A refund credits that same expense — the expense shrinks.
    assert increases_balance(AccountType.EXPENSE, EntrySide.CREDIT) is False
    # Being paid credits revenue — revenue grows.
    assert increases_balance(AccountType.REVENUE, EntrySide.CREDIT) is True


def test_signed_for_reporting_restates_bank_signs_as_account_movement() -> None:
    spend = Decimal("-412.55")
    refund = Decimal("412.55")
    payment = Decimal("25000.00")

    # A negative bank amount is a positive expense incurred.
    assert signed_for_reporting(AccountType.EXPENSE, EntrySide.DEBIT, spend) == Decimal("412.55")
    # A refund reduces that expense.
    assert signed_for_reporting(AccountType.EXPENSE, EntrySide.CREDIT, refund) == Decimal("-412.55")
    # Revenue keeps its sign: money in is income earned.
    assert signed_for_reporting(AccountType.REVENUE, EntrySide.CREDIT, payment) == Decimal("25000")


def test_a_money_out_transaction_debits_whatever_it_is_categorized_to() -> None:
    """Money out is not always an expense — but it is always a debit."""
    spend = Decimal("-5000")
    side = entry_side_for_amount(spend, AccountType.ASSET)
    assert side is EntrySide.DEBIT
    # Owner draw (equity) and loan repayment (liability) both shrink on a debit.
    assert increases_balance(AccountType.EQUITY, side) is False
    assert increases_balance(AccountType.LIABILITY, side) is False


# --- the default chart ---------------------------------------------------


def test_default_chart_covers_every_account_type() -> None:
    assert set(chart_summary()) == {t.value for t in AccountType}


def test_default_chart_has_no_duplicate_names() -> None:
    names = [entry.name.lower() for entry in DEFAULT_CHART]
    assert len(names) == len(set(names))


def test_every_seed_category_is_described_for_the_agent() -> None:
    """Descriptions go into the prompt, so a blank one is a silent quality bug."""
    for entry in DEFAULT_CHART:
        assert entry.description.strip(), f"{entry.name} has no description"
        assert entry.description.endswith("."), f"{entry.name}'s description is not a sentence"
