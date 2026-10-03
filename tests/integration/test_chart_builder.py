"""Step one of a cold start: the buckets, before anything is sorted into them.

Categorization can only be as good as the chart it chooses from, and a chart
is a structural decision — so the properties worth pinning are about what
reaches the model, what a human sees, and what is allowed to be written.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from books.agent.bootstrap import bootstrap_business
from books.agent.chart_builder import (
    ChartProposal,
    ProposedCategory,
    apply_proposal,
    build_chart,
    propose_chart,
)
from books.core import repository as repo
from books.core.accounting import AccountType
from books.core.errors import ValidationError

pytestmark = pytest.mark.db


def proposer_returning(*categories: ProposedCategory):
    """A stand-in model that records the prompt it was given."""

    async def propose(system: str, prompt: str) -> ChartProposal:
        propose.calls = getattr(propose, "calls", [])  # type: ignore[attr-defined]
        propose.calls.append(prompt)  # type: ignore[attr-defined]
        return ChartProposal(categories=list(categories))

    return propose


def expense(name: str, covers: list[str] | None = None) -> ProposedCategory:
    return ProposedCategory(
        name=name,
        account_type=AccountType.EXPENSE,
        description=f"Spending on {name.lower()}",
        covers=covers or [],
    )


async def _tx(
    session,
    business,
    vendor: str,
    *,
    amount: str = "-10.00",
    day: int = 1,
    description: str | None = None,
) -> None:
    account = (await repo.list_accounts(session, business_id=business.id))[0]
    await repo.upsert_transaction(
        session,
        values={
            "tenant_id": business.tenant_id,
            "business_id": business.id,
            "account_id": account.id,
            "provider_transaction_id": f"{vendor}-{day}-{amount}",
            "amount": Decimal(amount),
            "date": date(2026, 2, day),
            "vendor": vendor,
            "description": description if description is not None else f"{vendor} charge",
        },
    )
    await session.flush()


# --- what the model is shown --------------------------------------------


async def test_the_proposal_is_built_from_the_business_s_own_merchants(
    session, business, linked_connection
):
    """The whole reason for this step: a chart shaped by what actually landed."""
    await _tx(session, business, "AWS", day=1)
    await _tx(session, business, "Gusto", day=2)
    await session.commit()

    propose = proposer_returning(expense("Software & Subscriptions"))
    _, proposed, seen = await propose_chart(session, business_id=business.id, proposer=propose)

    prompt = propose.calls[0]  # type: ignore[attr-defined]
    assert "AWS" in prompt and "Gusto" in prompt
    assert seen == 2
    assert [p.name for p in proposed] == ["Software & Subscriptions"]


async def test_already_categorized_transactions_still_shape_the_chart(
    session, business, chart_of_accounts, linked_connection
):
    """Unlike the categorization pass, this looks at everything.

    A business that has been partly sorted already still needs its chart to
    cover those merchants, or re-running this would propose deleting nothing
    and covering nothing.
    """
    await _tx(session, business, "AWS", day=1)
    await session.commit()
    transaction = (
        await repo.list_transactions(session, repo.TransactionFilters(business_id=business.id))
    )[0]
    from books.core.categorization import apply_category

    await apply_category(
        session,
        transaction=transaction,
        category_id=chart_of_accounts["Software & Subscriptions"].id,
        actor="human",
        confidence=1.0,
        rationale="known",
        mark_reviewed=True,
    )
    await session.commit()

    propose = proposer_returning(expense("Travel"))
    _, _, seen = await propose_chart(session, business_id=business.id, proposer=propose)
    assert seen == 1
    assert "AWS" in propose.calls[0]  # type: ignore[attr-defined]


async def test_money_in_and_money_out_are_shown_as_separate_sections(
    session, business, linked_connection
):
    """The mistake this prevents was a real one, not a hypothetical.

    With one flat list the model read a row of familiar retailers and proposed
    a *revenue* account for them, though every line was money leaving the
    business. Direction has to be a heading, not a field at the end of a line.
    """
    await _tx(session, business, "IKEA", amount="-80.00", day=1)
    await _tx(session, business, "Acme Corp", amount="4000.00", day=2)
    await session.commit()

    propose = proposer_returning(expense("Office Furniture"))
    await propose_chart(session, business_id=business.id, proposer=propose)

    prompt = propose.calls[0]  # type: ignore[attr-defined]
    out, incoming = prompt.index("MONEY OUT"), prompt.index("MONEY IN")
    assert out < prompt.index("IKEA") < incoming
    assert incoming < prompt.index("Acme Corp")


async def test_existing_accounts_are_shown_so_they_are_not_proposed_again(
    session, business, chart_of_accounts, linked_connection
):
    await _tx(session, business, "AWS")
    await session.commit()

    propose = proposer_returning(expense("Travel"))
    await propose_chart(session, business_id=business.id, proposer=propose)
    prompt = propose.calls[0]  # type: ignore[attr-defined]
    for name in chart_of_accounts:
        assert name in prompt


async def test_a_business_with_nothing_synced_is_told_to_sync_first(session, business):
    """Better than proposing a chart out of thin air — that is what seed is for."""
    with pytest.raises(ValidationError, match="no transactions"):
        await propose_chart(
            session, business_id=business.id, proposer=proposer_returning(expense("Travel"))
        )


# --- what survives into the chart ---------------------------------------


async def test_a_name_the_business_already_has_is_never_proposed(
    session, business, chart_of_accounts, linked_connection
):
    """Asking the model for gaps is not the same as enforcing it.

    A near-duplicate account is worse than a missing one: money splits across
    two buckets that mean the same thing and nobody notices.
    """
    await _tx(session, business, "AWS")
    await session.commit()

    propose = proposer_returning(
        expense("travel"),  # differs from the existing "Travel" only in case
        expense("Meals & Entertainment"),
    )
    _, proposed, _ = await propose_chart(session, business_id=business.id, proposer=propose)
    assert [p.name for p in proposed] == ["Meals & Entertainment"]


async def test_a_repeated_proposal_is_only_created_once(session, business, linked_connection):
    await _tx(session, business, "AWS")
    await session.commit()

    propose = proposer_returning(expense("Travel"), expense("Travel"))
    result = await build_chart(session, business_id=business.id, proposer=propose, apply=True)
    assert [c.name for c in result.created] == ["Travel"]


async def test_proposing_writes_nothing(session, business, linked_connection):
    """The default has to be safe: a chart appears only when someone says so."""
    await _tx(session, business, "AWS")
    await session.commit()

    result = await build_chart(
        session, business_id=business.id, proposer=proposer_returning(expense("Travel"))
    )
    assert result.proposed and not result.created
    assert await repo.list_categories(session, business_id=business.id) == []


async def test_applying_creates_the_accounts_with_their_types(session, business, linked_connection):
    await _tx(session, business, "AWS")
    await session.commit()

    result = await build_chart(
        session,
        business_id=business.id,
        apply=True,
        proposer=proposer_returning(
            expense("Software & Subscriptions", covers=["AWS"]),
            ProposedCategory(
                name="Owner Draw",
                account_type=AccountType.EQUITY,
                description="Money the owner took out",
            ),
        ),
    )
    await session.commit()

    rows = await repo.list_categories(session, business_id=business.id)
    created = {c.name: c.account_type for c in rows}
    assert created == {
        "Software & Subscriptions": AccountType.EXPENSE,
        "Owner Draw": AccountType.EQUITY,
    }
    assert result.merchants_seen == 1


async def test_applying_twice_keeps_the_first_chart(session, business, linked_connection):
    """Re-running is a normal thing to do after a later sync."""
    await _tx(session, business, "AWS")
    await session.commit()
    propose = proposer_returning(expense("Travel"))

    await build_chart(session, business_id=business.id, proposer=propose, apply=True)
    await session.commit()
    second = await build_chart(session, business_id=business.id, proposer=propose, apply=True)
    await session.commit()

    assert second.created == []
    rows = await repo.list_categories(session, business_id=business.id)
    assert [r.name for r in rows] == ["Travel"]


async def test_applying_an_edited_proposal_creates_exactly_that(
    session, business, linked_connection
):
    """What a human approved is what gets written — not a second opinion."""
    await _tx(session, business, "AWS")
    await session.commit()

    result = await apply_proposal(session, business=business, proposed=[expense("Cloud Hosting")])
    await session.commit()
    assert [c.name for c in result.created] == ["Cloud Hosting"]


# --- the ordering between the two steps ----------------------------------


async def test_categorizing_before_the_chart_exists_says_which_command_to_run(
    session, business, linked_connection
):
    await _tx(session, business, "AWS")
    await session.commit()

    with pytest.raises(ValidationError, match="category bootstrap"):
        await bootstrap_business(session, business_id=business.id)


async def test_the_chart_it_builds_is_one_the_categorizer_can_use(
    session, business, linked_connection
):
    """The two steps have to meet: step one's names are step two's only options."""
    await _tx(session, business, "AWS", day=1)
    await _tx(session, business, "Delta", day=2)
    await session.commit()

    await build_chart(
        session,
        business_id=business.id,
        apply=True,
        proposer=proposer_returning(expense("Software & Subscriptions"), expense("Travel")),
    )
    await session.commit()

    from books.agent.categorizer import Classification

    async def classify(system: str, prompt: str) -> Classification:
        name = "Travel" if "delta" in prompt.lower() else "Software & Subscriptions"
        return Classification(category_name=name, confidence=0.9, rationale="fits")

    result = await bootstrap_business(session, business_id=business.id, classifier=classify)
    assert result.transactions_categorized == 2
    assert not [d for d in result.decisions if d.error]
