"""Vendor normalization — the key every merchant-level decision groups on.

Two failure directions, and they are not symmetric. Failing to merge two
spellings of one merchant costs an extra human decision. Merging two *different*
merchants silently mis-categorizes money, and nobody sees it happen. These
tests pin both, with more weight on the second.
"""

from __future__ import annotations

import pytest

from books.core.vendors import is_payment_rail, normalize_vendor


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Processor prefixes are the processor's, not the merchant's.
        ("SQ *BLUE BOTTLE", "BLUE BOTTLE"),
        ("SQ*BLUE BOTTLE", "BLUE BOTTLE"),
        ("TST* SHAKE SHACK", "SHAKE SHACK"),
        ("PAYPAL *ETSY SELLER", "ETSY SELLER"),
        # Store, terminal and reference numbers vary per visit.
        ("THE HOME DEPOT #6127", "THE HOME DEPOT"),
        ("SHELL OIL 574412", "SHELL OIL"),
        # Punctuation that differs between statements for one merchant.
        ("DOMINO'S PIZZA", "DOMINOS PIZZA"),
        ("U.S. BANK", "US BANK"),
        # Case and spacing are never identity.
        ("  costco   wholesale ", "COSTCO WHOLESALE"),
    ],
)
def test_spellings_of_one_merchant_collapse_together(raw: str, expected: str) -> None:
    assert normalize_vendor(raw) == expected


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("DOMINO'S PIZZA", "DOMINOS PIZZA"),
        ("SQ *BLUE BOTTLE #417", "Blue Bottle"),
        ("KOHL'S", "kohls"),
    ],
)
def test_variants_share_a_key(left: str, right: str) -> None:
    assert normalize_vendor(left) == normalize_vendor(right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # The expensive mistake: two real, different merchants must not merge.
        ("SHELL", "SHELL OIL CHANGE PLUS"),
        ("AMERICAN AIRLINES", "AMERICAN EXPRESS"),
        ("BANK OF THE WEST", "BANK OF EURASIA"),
        ("UBER", "UBER EATS"),
    ],
)
def test_different_merchants_stay_apart(left: str, right: str) -> None:
    assert normalize_vendor(left) != normalize_vendor(right)


@pytest.mark.parametrize(
    "rail",
    [
        "Incoming Wire",
        "Domestic Wire",
        "Zelle Payment",
        "Cash Deposit",
        "ACH Debit",
        "Transfer",
        "ATM Withdrawal",
    ],
)
def test_payment_rails_are_not_merchants(rail: str) -> None:
    """A rail says how money moved, never to whom.

    Grouping on it would put every unrelated wire in one bucket — which is
    exactly what the old description-prefix matching did.
    """
    assert is_payment_rail(rail) is True
    assert normalize_vendor(None, rail) is None


def test_a_real_counterparty_wins_over_the_rail_in_the_description() -> None:
    """Teller shapes like "AB LOGISTICS ||| Zelle Payment" are common."""
    assert normalize_vendor("AB LOGISTICS", "Zelle Payment") == "AB LOGISTICS"


def test_description_is_only_a_fallback_when_it_names_someone() -> None:
    assert normalize_vendor(None, "Stripe Payout") == "STRIPE PAYOUT"
    assert normalize_vendor(None, "Incoming Wire") is None


@pytest.mark.parametrize("empty", [None, "", "   ", "***", "###"])
def test_nothing_to_key_on_returns_none(empty: str | None) -> None:
    assert normalize_vendor(empty) is None


# --- the description, where the merchant alone is not enough -------------


@pytest.mark.parametrize(
    ("description", "vendor", "expected"),
    [
        # Real shapes from one statement, same merchant and both money out:
        # a liability payment and an expense.
        ("American Express Platinum Card", "AMERICAN EXPRESS", "AMERICAN EXPRESS PLATINUM CARD"),
        ("Interest Payment", "AMERICAN EXPRESS", "INTEREST PAYMENT"),
        # A description that only restates the merchant discriminates nothing.
        ("Shell", "SHELL", ""),
        ("The Home Depot", "THE HOME DEPOT", ""),
        # Reference numbers and dates vary per row and must not split a group.
        ("Card ending 4412 on 03/14", "CHASE", "CARD ENDING ON"),
        (None, "SHELL", ""),
        ("", "SHELL", ""),
    ],
)
def test_description_keys(description: str | None, vendor: str, expected: str) -> None:
    from books.core.vendors import normalize_description

    assert normalize_description(description, vendor) == expected


def test_one_merchant_two_accounts_get_different_decision_keys() -> None:
    """The case the merchant key alone cannot separate: both are money out."""
    from books.core.vendors import decision_key

    card = decision_key("AMERICAN EXPRESS", "American Express Platinum Card", "debit")
    interest = decision_key("AMERICAN EXPRESS", "Interest Payment", "debit")
    assert card != interest


def test_money_in_and_money_out_are_different_decisions() -> None:
    """Paying a contractor and being paid back are not the same event."""
    from books.core.vendors import decision_key

    paid = decision_key("SAM BLOCK", "Zelle Payment", "debit")
    received = decision_key("SAM BLOCK", "Zelle Payment", "credit")
    assert paid != received


def test_a_restated_description_does_not_fragment_a_merchant() -> None:
    from books.core.vendors import decision_key

    assert decision_key("SHELL", "Shell", "debit") == decision_key("SHELL", "SHELL", "debit")
