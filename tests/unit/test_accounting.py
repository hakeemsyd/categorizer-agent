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
def test_entry_side_follows_the_sign_of_the_amount(amount, expected) -> None:
    assert entry_side_for_amount(Decimal(amount)) is expected


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
    side = entry_side_for_amount(spend)
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
