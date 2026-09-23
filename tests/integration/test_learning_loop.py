"""The quality loop: cold start, learning from humans, and propagating fixes.

Three things have to hold for categorization to get better rather than just
stay busy:

1. A fresh import with no history gets categorized at all, consistently.
2. Human-verified decisions become the evidence the agent reasons from.
3. Correcting one row fixes the others like it, without touching anything a
   human already ruled on.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from books.agent.bootstrap import bootstrap_business
from books.agent.categorizer import Classification, categorize_transaction
from books.core import repository as repo
from books.core.categorization import apply_category, propagate_correction
from books.core.errors import ValidationError
from books.core.models import Transaction

pytestmark = pytest.mark.db


def classifier_returning(name: str, confidence: float = 0.9, rationale: str = "because"):
    async def classify(system: str, prompt: str) -> Classification:
        classify.calls = getattr(classify, "calls", [])  # type: ignore[attr-defined]
        classify.calls.append(prompt)  # type: ignore[attr-defined]
        return Classification(category_name=name, confidence=confidence, rationale=rationale)

    return classify


def classifier_by_vendor(mapping: dict[str, str], default: str = "Other Expense"):
    """Answers differently per merchant, so grouping is observable."""

    async def classify(system: str, prompt: str) -> Classification:
        classify.calls = getattr(classify, "calls", [])  # type: ignore[attr-defined]
        classify.calls.append(prompt)  # type: ignore[attr-defined]
        for vendor, category in mapping.items():
            if vendor.lower() in prompt.lower():
                return Classification(
                    category_name=category, confidence=0.9, rationale=f"looks like {vendor}"
                )
        return Classification(category_name=default, confidence=0.3, rationale="unfamiliar")

    return classify


async def _tx(session, business, vendor: str, amount: str = "-10.00", day: int = 1) -> Transaction:
    account = (await repo.list_accounts(session, business_id=business.id))[0]
    transaction_id, _ = await repo.upsert_transaction(
        session,
        values={
            "tenant_id": business.tenant_id,
            "business_id": business.id,
            "account_id": account.id,
            "provider_transaction_id": f"{vendor}-{day}-{amount}",
            "amount": Decimal(amount),
            "date": date(2026, 2, day),
            "vendor": vendor,
            "description": f"{vendor} charge",
        },
    )
    await session.flush()
    return await repo.get_transaction(session, transaction_id)


# --- 1. cold start -------------------------------------------------------


async def test_bootstrap_decides_once_per_merchant_not_once_per_transaction(
    session, business, chart_of_accounts, linked_item
):
    """The point of the vendor-first pass: fewer calls, consistent answers."""
    for day in range(1, 4):
        await _tx(session, business, "AWS", day=day)
    for day in range(1, 3):
        await _tx(session, business, "Delta", day=day)
    await session.commit()

    classify = classifier_by_vendor({"AWS": "Software & Subscriptions", "Delta": "Travel"})
    result = await bootstrap_business(session, business_id=business.id, classifier=classify)

    # Two merchants, five transactions, two model calls.
    assert result.vendors_seen == 2
    assert result.vendors_decided == 2
    assert result.transactions_categorized == 5
    assert len(classify.calls) == 2  # type: ignore[attr-defined]


async def test_bootstrap_gives_every_transaction_from_one_merchant_the_same_category(
    session, business, chart_of_accounts, linked_item
):
    for day in range(1, 4):
        await _tx(session, business, "AWS", day=day)
    await session.commit()

    await bootstrap_business(
        session,
        business_id=business.id,
        classifier=classifier_returning("Software & Subscriptions"),
    )

    rows = await repo.list_transactions(
        session, repo.TransactionFilters(business_id=business.id, search="aws", limit=50)
    )
    categories = {r.category_id for r in rows}
    assert len(categories) == 1, "one merchant landed in more than one category"
    assert chart_of_accounts["Software & Subscriptions"].id in categories


async def test_bootstrap_leaves_everything_unreviewed(
    session, business, chart_of_accounts, linked_item
):
    """It is still the model's opinion — it must not claim human approval."""
    await _tx(session, business, "AWS")
    await session.commit()

    await bootstrap_business(
        session,
        business_id=business.id,
        classifier=classifier_returning("Software & Subscriptions"),
    )

    rows = await repo.list_transactions(
        session, repo.TransactionFilters(business_id=business.id, limit=50)
    )
    assert all(r.last_reviewed_at is None for r in rows)


async def test_bootstrap_skips_a_merchant_it_cannot_place(
    session, business, chart_of_accounts, linked_item
):
    """An invented category is refused here exactly as in the single-row path."""
    await _tx(session, business, "MYSTERY CO")
    await session.commit()

    result = await bootstrap_business(
        session,
        business_id=business.id,
        classifier=classifier_returning("Interdimensional Freight"),
    )

    assert result.vendors_decided == 0
    assert result.transactions_categorized == 0
    assert result.decisions[0].error == "invalid_category"
    rows = await repo.list_transactions(
        session, repo.TransactionFilters(business_id=business.id, limit=50)
    )
    assert all(r.category_id is None for r in rows)


async def test_bootstrap_uses_a_standing_rule_instead_of_paying_for_a_call(
    session, business, chart_of_accounts, linked_item
):
    await repo.create_rule(
        session,
        business=business,
        match_type="vendor_equals",
        pattern="AWS",
        category_id=chart_of_accounts["Software & Subscriptions"].id,
    )
    await _tx(session, business, "AWS")
    await session.commit()

    async def never_called(system: str, prompt: str) -> Classification:
        raise AssertionError("a standing rule already answers this merchant")

    result = await bootstrap_business(session, business_id=business.id, classifier=never_called)
    assert result.transactions_categorized == 1


async def test_bootstrap_without_a_chart_of_accounts_says_so(session, business, linked_item):
    await _tx(session, business, "AWS")
    await session.commit()

    with pytest.raises(ValidationError, match="chart of accounts"):
        await bootstrap_business(
            session, business_id=business.id, classifier=classifier_returning("Anything")
        )


async def test_bootstrap_ignores_rows_with_no_identifiable_merchant(
    session, business, chart_of_accounts, linked_item
):
    """A bare wire has nothing to generalize from — leave it to the row path."""
    account = (await repo.list_accounts(session, business_id=business.id))[0]
    transaction_id, _ = await repo.upsert_transaction(
        session,
        values={
            "tenant_id": business.tenant_id,
            "business_id": business.id,
            "account_id": account.id,
            "provider_transaction_id": "bare-wire",
            "amount": Decimal("-500.00"),
            "date": date(2026, 2, 1),
            "vendor": None,
            "description": "Incoming Wire",
        },
    )
    await session.flush()
    await session.commit()

    result = await bootstrap_business(
        session, business_id=business.id, classifier=classifier_returning("Other Expense")
    )
    assert result.vendors_seen == 0
    assert (await repo.get_transaction(session, transaction_id)).category_id is None


# --- 2. learning from verified examples ----------------------------------


async def test_a_human_correction_reaches_the_prompt_as_a_correction(
    session, business, chart_of_accounts, linked_item
):
    """The strongest signal available: what the agent got wrong, and the fix."""
    wrong = await _tx(session, business, "AWS", day=1)
    await apply_category(
        session,
        transaction=wrong,
        category_id=chart_of_accounts["Travel"].id,
        actor="agent:categorizer-v1",
        confidence=0.6,
    )
    await apply_category(
        session,
        transaction=wrong,
        category_id=chart_of_accounts["Software & Subscriptions"].id,
        actor="cli:hakeem",
        confidence=1.0,
        mark_reviewed=True,
    )

    later = await _tx(session, business, "AWS", day=9)
    classify = classifier_returning("Software & Subscriptions")
    await categorize_transaction(session, transaction_id=later.id, classifier=classify)

    prompt = classify.calls[-1]  # type: ignore[attr-defined]
    assert "Corrections a human has already made" in prompt
    assert "the agent said Travel" in prompt
    assert "a human changed it to Software & Subscriptions" in prompt


async def test_the_prompt_separates_verified_examples_from_the_agents_own_guesses(
    session, business, chart_of_accounts, linked_item
):
    guessed = await _tx(session, business, "AWS", day=1)
    await apply_category(
        session,
        transaction=guessed,
        category_id=chart_of_accounts["Travel"].id,
        actor="agent:categorizer-v1",
        confidence=0.9,
    )

    later = await _tx(session, business, "AWS", day=9)
    classify = classifier_returning("Travel")
    await categorize_transaction(session, transaction_id=later.id, classifier=classify)

    prompt = classify.calls[-1]  # type: ignore[attr-defined]
    assert "(unreviewed guess)" in prompt
    assert "(human-reviewed)" not in prompt


async def test_examples_are_matched_on_the_merchant_not_the_payment_rail(
    session, business, chart_of_accounts, linked_item
):
    """Regression: matching the description's first word pulled in every wire.

    Two unrelated counterparties that share the rail "Domestic Wire" must not
    be offered to each other as precedent.
    """
    account = (await repo.list_accounts(session, business_id=business.id))[0]
    for vendor in ("ACME CORP", "VC FUND"):
        transaction_id, _ = await repo.upsert_transaction(
            session,
            values={
                "tenant_id": business.tenant_id,
                "business_id": business.id,
                "account_id": account.id,
                "provider_transaction_id": f"wire-{vendor}",
                "amount": Decimal("5000.00"),
                "date": date(2026, 2, 1),
                "vendor": vendor,
                "description": "Domestic Wire",
            },
        )
        await session.flush()
        if vendor == "ACME CORP":
            await apply_category(
                session,
                transaction=await repo.get_transaction(session, transaction_id),
                category_id=chart_of_accounts["Consulting Revenue"].id,
                actor="cli:hakeem",
                confidence=1.0,
                mark_reviewed=True,
            )

    vc_fund = (
        await repo.list_transactions(
            session, repo.TransactionFilters(business_id=business.id, search="vc fund", limit=5)
        )
    )[0]
    similar = await repo.find_similar_transactions(session, transaction=vc_fund, limit=10)
    assert similar == [], "an unrelated wire was offered as a precedent"


# --- 3. propagating a correction -----------------------------------------


async def test_correcting_one_row_fixes_the_others_from_that_merchant(
    session, business, chart_of_accounts, linked_item
):
    rows = [await _tx(session, business, "AWS", day=d) for d in range(1, 4)]
    for row in rows:
        await apply_category(
            session,
            transaction=row,
            category_id=chart_of_accounts["Travel"].id,
            actor="agent:categorizer-v1",
            confidence=0.7,
        )

    corrected = rows[0]
    await apply_category(
        session,
        transaction=corrected,
        category_id=chart_of_accounts["Software & Subscriptions"].id,
        actor="cli:hakeem",
        confidence=1.0,
        mark_reviewed=True,
    )
    propagation = await propagate_correction(session, transaction=corrected, actor="cli:hakeem")

    assert propagation.count == 2
    for row in rows[1:]:
        await session.refresh(row)
        assert row.category_id == chart_of_accounts["Software & Subscriptions"].id


async def test_propagation_never_overwrites_another_humans_decision(
    session, business, chart_of_accounts, linked_item
):
    first = await _tx(session, business, "AWS", day=1)
    deliberate = await _tx(session, business, "AWS", day=2)
    await apply_category(
        session,
        transaction=deliberate,
        category_id=chart_of_accounts["Travel"].id,
        actor="cli:someone-else",
        confidence=1.0,
        mark_reviewed=True,
    )

    await apply_category(
        session,
        transaction=first,
        category_id=chart_of_accounts["Software & Subscriptions"].id,
        actor="cli:hakeem",
        confidence=1.0,
        mark_reviewed=True,
    )
    propagation = await propagate_correction(session, transaction=first, actor="cli:hakeem")

    await session.refresh(deliberate)
    assert deliberate.category_id == chart_of_accounts["Travel"].id
    assert propagation.count == 0
    assert propagation.skipped_reviewed == 1


async def test_propagation_stops_at_the_merchant_boundary(
    session, business, chart_of_accounts, linked_item
):
    aws = await _tx(session, business, "AWS", day=1)
    delta = await _tx(session, business, "Delta", day=1)
    await apply_category(
        session,
        transaction=delta,
        category_id=chart_of_accounts["Travel"].id,
        actor="agent:categorizer-v1",
        confidence=0.8,
    )

    await apply_category(
        session,
        transaction=aws,
        category_id=chart_of_accounts["Software & Subscriptions"].id,
        actor="cli:hakeem",
        confidence=1.0,
        mark_reviewed=True,
    )
    await propagate_correction(session, transaction=aws, actor="cli:hakeem")

    await session.refresh(delta)
    assert delta.category_id == chart_of_accounts["Travel"].id


async def test_propagated_rows_are_attributed_honestly(
    session, business, chart_of_accounts, linked_item
):
    """The human never saw these rows — history must not imply they did."""
    first = await _tx(session, business, "AWS", day=1)
    peer = await _tx(session, business, "AWS", day=2)

    await apply_category(
        session,
        transaction=first,
        category_id=chart_of_accounts["Software & Subscriptions"].id,
        actor="cli:hakeem",
        confidence=1.0,
        mark_reviewed=True,
    )
    await propagate_correction(session, transaction=first, actor="cli:hakeem")

    await session.refresh(peer)
    assert peer.last_reviewed_at is None, "propagation must not claim human review"
    history = await repo.list_history(session, transaction_id=peer.id)
    assert history[0].actor == "propagation:cli:hakeem"


async def test_propagation_is_a_no_op_when_the_merchant_has_one_transaction(
    session, business, chart_of_accounts, linked_item
):
    only = await _tx(session, business, "AWS")
    await apply_category(
        session,
        transaction=only,
        category_id=chart_of_accounts["Software & Subscriptions"].id,
        actor="cli:hakeem",
        confidence=1.0,
        mark_reviewed=True,
    )
    propagation = await propagate_correction(session, transaction=only, actor="cli:hakeem")
    assert propagation.count == 0


# --- the description, where the merchant alone is too coarse -------------


async def _tx_desc(session, business, vendor, description, amount, day=1) -> Transaction:
    account = (await repo.list_accounts(session, business_id=business.id))[0]
    transaction_id, _ = await repo.upsert_transaction(
        session,
        values={
            "tenant_id": business.tenant_id,
            "business_id": business.id,
            "account_id": account.id,
            "provider_transaction_id": f"{vendor}-{description}-{amount}-{day}",
            "amount": Decimal(amount),
            "date": date(2026, 2, day),
            "vendor": vendor,
            "description": description,
        },
    )
    await session.flush()
    return await repo.get_transaction(session, transaction_id)


async def test_a_correction_does_not_cross_a_direction_flip(
    session, business, chart_of_accounts, linked_item
):
    """Regression, from real data: SAM BLOCK is 4 payments out and 1 in.

    Categorizing the outgoing ones as contractor spend must not also relabel
    the money that came back — that is a different event entirely.
    """
    paid = await _tx_desc(session, business, "SAM BLOCK", "Zelle Payment", "-117.65", day=1)
    also_paid = await _tx_desc(session, business, "SAM BLOCK", "Zelle Payment", "-47.54", day=2)
    received = await _tx_desc(session, business, "SAM BLOCK", "Zelle Payment", "43.40", day=3)

    await apply_category(
        session,
        transaction=paid,
        category_id=chart_of_accounts["Payroll"].id,
        actor="cli:hakeem",
        confidence=1.0,
        mark_reviewed=True,
    )
    propagation = await propagate_correction(session, transaction=paid, actor="cli:hakeem")

    await session.refresh(also_paid)
    await session.refresh(received)
    assert also_paid.category_id == chart_of_accounts["Payroll"].id  # same direction: reached
    assert received.category_id is None  # money in: left alone
    assert propagation.count == 1


async def test_a_correction_does_not_cross_a_different_kind_of_line(
    session, business, chart_of_accounts, linked_item
):
    """Regression, from real data: AMERICAN EXPRESS bills a card payment and
    interest on the same card, both money out. They are different accounts,
    and only the description says so."""
    card = await _tx_desc(
        session, business, "AMERICAN EXPRESS", "American Express Platinum Card", "-85.78", day=1
    )
    interest = await _tx_desc(
        session, business, "AMERICAN EXPRESS", "Interest Payment", "-17.17", day=2
    )

    await apply_category(
        session,
        transaction=card,
        category_id=chart_of_accounts["Credit Card Payable"].id,
        actor="cli:hakeem",
        confidence=1.0,
        mark_reviewed=True,
    )
    await propagate_correction(session, transaction=card, actor="cli:hakeem")

    await session.refresh(interest)
    assert interest.category_id is None, "interest was relabelled as a card payment"


async def test_a_restated_description_still_propagates_across_the_merchant(
    session, business, chart_of_accounts, linked_item
):
    """The narrowing must not be so strict that normal merchants stop grouping.

    "SHELL | Shell" restates the merchant, so it discriminates nothing and
    those rows stay one group.
    """
    first = await _tx_desc(session, business, "SHELL", "Shell", "-40.00", day=1)
    second = await _tx_desc(session, business, "SHELL", "SHELL", "-55.00", day=2)

    await apply_category(
        session,
        transaction=first,
        category_id=chart_of_accounts["Travel"].id,
        actor="cli:hakeem",
        confidence=1.0,
        mark_reviewed=True,
    )
    propagation = await propagate_correction(session, transaction=first, actor="cli:hakeem")

    await session.refresh(second)
    assert second.category_id == chart_of_accounts["Travel"].id
    assert propagation.count == 1


async def test_bootstrap_decides_separately_for_each_kind_of_line(
    session, business, chart_of_accounts, linked_item
):
    """One merchant, two accounts — so two decisions, not one blurred answer."""
    await _tx_desc(
        session, business, "AMERICAN EXPRESS", "American Express Platinum Card", "-85.78", day=1
    )
    await _tx_desc(session, business, "AMERICAN EXPRESS", "Interest Payment", "-17.17", day=2)
    await session.commit()

    async def by_description(system: str, prompt: str) -> Classification:
        # Match the group label, not a bare substring: the prompt's own
        # guidance text mentions "Interest Payment" as an example.
        if "(Interest Payment," in prompt:
            return Classification(
                category_name="Interest Expense", confidence=0.9, rationale="interest"
            )
        return Classification(
            category_name="Credit Card Payable", confidence=0.9, rationale="card payment"
        )

    result = await bootstrap_business(session, business_id=business.id, classifier=by_description)

    assert result.vendors_seen == 2, "one merchant collapsed into a single decision"
    rows = await repo.list_transactions(
        session,
        repo.TransactionFilters(business_id=business.id, search="american express", limit=10),
    )
    by_desc = {r.description: r.category_id for r in rows}
    assert by_desc["Interest Payment"] == chart_of_accounts["Interest Expense"].id
    assert by_desc["American Express Platinum Card"] == chart_of_accounts["Credit Card Payable"].id
