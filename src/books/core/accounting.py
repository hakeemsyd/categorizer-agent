"""Accounting primitives: account types, normal balances, and entry sides.

The rules here are the standard ones, stated once so nothing else has to
restate them:

* Every category is one of the five fundamental account types.
* Each type has a **normal balance** — the side that *increases* it. Assets and
  expenses increase on the debit side; liabilities, equity and revenue increase
  on the credit side.
* Each transaction applies one **entry side** to its category. The bank account
  takes the opposite side; that is the other half of the entry.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum


class AccountType(StrEnum):
    """The five fundamentals of a chart of accounts."""

    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    REVENUE = "revenue"
    EXPENSE = "expense"


class EntrySide(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"


#: The side that increases each account type.
NORMAL_BALANCE: dict[AccountType, EntrySide] = {
    AccountType.ASSET: EntrySide.DEBIT,
    AccountType.EXPENSE: EntrySide.DEBIT,
    AccountType.LIABILITY: EntrySide.CREDIT,
    AccountType.EQUITY: EntrySide.CREDIT,
    AccountType.REVENUE: EntrySide.CREDIT,
}

#: Account types a *bank* account can be: cash held, or credit owed.
BANK_ACCOUNT_TYPES = (AccountType.ASSET, AccountType.LIABILITY)


def normal_balance(account_type: AccountType | str) -> EntrySide:
    """The side that increases this account type."""
    return NORMAL_BALANCE[AccountType(account_type)]


def entry_side_for_amount(amount: Decimal | float | int) -> EntrySide:
    """The side a transaction applies to its *category*.

    One convention, enforced at the boundary: **a negative amount is money
    leaving the business**, whatever kind of account it sits on. Normalizing
    to that is the provider adapter's job (see RawTransaction.amount), and it
    has to be, because the sign a provider reports is its own choice. Teller
    signed from the bank account's point of view, so a credit-card purchase
    arrived positive; Fintable normalizes first, so the same purchase arrives
    negative.

    This function deliberately does **not** look at the account type. It used
    to, inverting the sign for liabilities — correct for Teller, and silently
    wrong for Fintable, where it double-inverted and booked 903 card purchases
    as income. Account-shaped special cases belong in the one adapter that
    needs them, not in a rule every transaction passes through.

    Given the direction, the category takes the opposite side from the bank
    account:

    * money out -> credit the bank account, **debit** the category
      (buying software debits an expense; an owner draw debits equity)
    * money in  -> debit the bank account, **credit** the category
      (a client payment credits revenue; an expense refund credits that expense)

    A zero-amount transaction is recorded as a credit; it moves no balance
    either way.
    """
    return EntrySide.DEBIT if Decimal(str(amount)) < 0 else EntrySide.CREDIT


def increases_balance(account_type: AccountType | str, entry_side: EntrySide | str) -> bool:
    """Does this entry grow the account, or shrink it?

    Reporting needs this: a credit to Revenue is income earned, while a credit
    to an expense (a refund) reduces the expense.
    """
    return normal_balance(account_type) is EntrySide(entry_side)


def signed_for_reporting(
    account_type: AccountType | str, entry_side: EntrySide | str, amount: Decimal
) -> Decimal:
    """``amount`` restated so that positive always means "grew this account".

    Transaction amounts are signed from the bank's point of view, which flips
    for revenue: a 25,000 client payment is +25,000 in the bank and also
    +25,000 of revenue earned, while a -412.55 AWS charge is +412.55 of expense
    incurred.
    """
    magnitude = abs(Decimal(str(amount)))
    return magnitude if increases_balance(account_type, entry_side) else -magnitude
