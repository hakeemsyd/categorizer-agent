"""Turning a bank's vendor string into a key you can group and match on.

One human decision should cover every transaction from the same merchant —
now and in the future. That only works if "SQ *BLUE BOTTLE #417" and
"BLUE BOTTLE COFFEE" collapse to the same key.

Deliberately conservative. Over-normalizing is the worse failure: merging two
genuinely different merchants silently mis-categorizes money, while leaving
them separate merely costs one extra human decision. Every rule here strips
something that is demonstrably *not* part of a merchant's identity — a
processor's prefix, a store number, a payment rail — and nothing else.

The categorizer reaches this through ``repository.find_similar_transactions``
and the vendor-grouping queries, so swapping in embeddings later means
changing those, not the agent.
"""

from __future__ import annotations

import re

# Payment processors and marketplaces prepend their own tag to the merchant.
# "SQ *BLUE BOTTLE" is Square's tag, not a merchant called SQ.
_PROCESSOR_PREFIXES = re.compile(
    r"""^(
        SQ\s*\*          # Square
      | TST\s*\*         # Toast
      | SP\s*\*          # Shopify / Stripe-hosted storefronts
      | PY\s*\*          # Paypal-style
      | PAYPAL\s*\*?     # PayPal
      | PP\s*\*
      | IC\s*\*          # Instacart
      | EB\s*\*          # Eventbrite
      | WWW\.            # bare URLs
      | POS\s+
      | PURCHASE\s+AUTHORIZED\s+ON\s+
      | RECURRING\s+PAYMENT\s+
      | ACH\s+(DEBIT|CREDIT)\s+
    )""",
    re.VERBOSE | re.IGNORECASE,
)

# Trailing store/terminal/reference numbers: "#417", "* 1234", " 000123".
_TRAILING_REF = re.compile(r"[\s*#-]+\d{2,}\s*$")

# Payment rails. These describe *how* money moved, never *who* it went to, so
# a description made only of these carries no vendor identity at all — see the
# "VC FUND ||| Incoming Wire" shape in real Teller data.
_RAIL_ONLY = re.compile(
    r"""^(
        (INCOMING|OUTGOING|DOMESTIC|INTERNATIONAL)?\s*WIRE(\s+TRANSFER)?
      | ZELLE(\s+PAYMENT)?
      | ACH(\s+(DEBIT|CREDIT|PAYMENT|TRANSFER))?
      | (CASH\s+)?(DEPOSIT|WITHDRAWAL)
      | TRANSFER
      | CHECK(\s+\#?\d*)?
      | ATM(\s+WITHDRAWAL)?
      | DIRECT\s+DEP(OSIT)?
      | ONLINE\s+PAYMENT
      | BILL\s+PAY(MENT)?
      | (DEBIT|CREDIT)\s+CARD\s+PAYMENT
    )$""",
    re.VERBOSE | re.IGNORECASE,
)

# Apostrophes and in-word periods vanish rather than becoming spaces, so
# "DOMINO'S PIZZA" and "DOMINOS PIZZA" — both real statement spellings of one
# merchant — land on the same key.
_ELIDED = re.compile(r"['`.]")
# Everything else punctuation-ish becomes a separator.
_NOISE = re.compile(r"[^A-Z0-9&\s]")
_WHITESPACE = re.compile(r"\s+")


def decision_key(
    vendor: str | None, description: str | None, entry_side: str | None
) -> tuple[str, str, str]:
    """How narrowly one human decision should be allowed to generalize.

    The merchant alone is too coarse to copy a category across. Two things at
    the same merchant routinely belong in different accounts:

    * **direction** — paying a contractor is an expense; money coming back
      from them is not the same event.
    * **what the line says** — "Platinum Card" and "Interest Payment" are both
      money out to American Express, and land in different accounts.

    So a decision propagates only within an identical triple. Splitting too
    finely just costs one more human decision; merging too coarsely silently
    misbooks money, and nobody sees it happen.
    """
    return (
        normalize_vendor(vendor, description) or "",
        (entry_side or ""),
        normalize_description(description, vendor),
    )


def is_payment_rail(text: str | None) -> bool:
    """Is this string only describing how the money moved, not to whom?

    Used to keep a description like "Domestic Wire" from being treated as a
    merchant name, which would group every unrelated wire together.
    """
    if not text:
        return False
    return bool(_RAIL_ONLY.match(_WHITESPACE.sub(" ", text.strip())))


def normalize_description(description: str | None, vendor: str | None = None) -> str:
    """What the description says *beyond* naming the merchant.

    The merchant alone is not always enough to pick an account. Real examples
    from one statement:

        AMERICAN EXPRESS | American Express Platinum Card  -> a liability
        AMERICAN EXPRESS | Interest Payment                -> an expense

    Both are money out from the same merchant, so direction cannot separate
    them — only this can. Returns "" when the description merely restates the
    merchant ("SHELL | Shell"), because then it discriminates nothing and
    should not split the merchant into spurious groups.
    """
    if not description or not description.strip():
        return ""

    text = _WHITESPACE.sub(" ", description.strip().upper())
    text = _ELIDED.sub("", text)
    text = _NOISE.sub(" ", text)
    # Reference numbers and dates differ per transaction and are never the
    # kind of thing that distinguishes one account from another.
    text = re.sub(r"\b\d+\b", " ", text)
    text = _WHITESPACE.sub(" ", text).strip()

    vendor_key = normalize_vendor(vendor) if vendor else None
    if vendor_key and text == vendor_key:
        return ""
    return text


def normalize_vendor(vendor: str | None, description: str | None = None) -> str | None:
    """A stable grouping key for one merchant, or None if there isn't one.

    Falls back to the description only when it actually names someone —
    a bare payment rail is not a merchant.
    """
    source = vendor if (vendor and vendor.strip()) else description
    if not source or not source.strip():
        return None
    if is_payment_rail(source):
        return None

    text = source.strip().upper()
    text = _PROCESSOR_PREFIXES.sub("", text)
    # Strip repeatedly: "MERCHANT #12 *3456" carries more than one tail.
    for _ in range(3):
        stripped = _TRAILING_REF.sub("", text)
        if stripped == text:
            break
        text = stripped
    text = _ELIDED.sub("", text)
    text = _NOISE.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()

    # Left with nothing meaningful (e.g. the vendor was only punctuation).
    return text or None
