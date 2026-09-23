"""The single write path, and the agent graph that uses it."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from books.agent.categorizer import Classification, categorize_transaction
from books.core import repository as repo
from books.core.categorization import apply_category, confirm_category
from books.core.errors import ValidationError
from books.core.models import Transaction
from books.core.sync import sync_item

pytestmark = pytest.mark.db


def classifier_returning(name: str, confidence: float = 0.95, rationale: str = "because"):
    async def classify(system: str, prompt: str) -> Classification:
        classify.last_prompt = prompt  # type: ignore[attr-defined]
        return Classification(category_name=name, confidence=confidence, rationale=rationale)

    return classify


async def _one_transaction(session, business, vendor: str = "AWS") -> Transaction:
    account = (await repo.list_accounts(session, business_id=business.id))[0]
    transaction_id, _ = await repo.upsert_transaction(
        session,
        values={
            "tenant_id": business.tenant_id,
            "business_id": business.id,
            "account_id": account.id,
            "provider_transaction_id": f"manual-{vendor}",
            "amount": Decimal("-412.55"),
            "date": date(2026, 2, 14),
            "vendor": vendor,
            "description": f"{vendor} monthly charge",
        },
    )
    await session.flush()
    return await repo.get_transaction(session, transaction_id)


# --- apply_category -----------------------------------------------------


async def test_apply_category_writes_history_every_time(
    session, business, chart_of_accounts, linked_item
):
    transaction = await _one_transaction(session, business)
    software = chart_of_accounts["Software & Subscriptions"]
    payroll = chart_of_accounts["Payroll"]

    await apply_category(
        session,
        transaction=transaction,
        category_id=software.id,
        actor="agent:categorizer-v1",
        confidence=0.9,
        rationale="Cloud vendor",
    )
    await apply_category(
        session,
        transaction=transaction,
        category_id=payroll.id,
        actor="cli:hakeem",
        confidence=1.0,
        rationale="Actually payroll",
        mark_reviewed=True,
    )

    history = await repo.list_history(session, transaction_id=transaction.id)
    assert [h.actor for h in history] == ["cli:hakeem", "agent:categorizer-v1"]
    assert [h.category_id for h in history] == [payroll.id, software.id]
    assert transaction.category_id == payroll.id


async def test_low_confidence_routes_to_review(session, business, chart_of_accounts, linked_item):
    transaction = await _one_transaction(session, business)
    await apply_category(
        session,
        transaction=transaction,
        category_id=chart_of_accounts["Travel"].id,
        actor="agent:categorizer-v1",
        confidence=0.42,
    )
    assert transaction.needs_review is True
    assert transaction.last_reviewed_at is None  # the agent never marks review


async def test_high_confidence_clears_review_but_not_reviewed_at(
    session, business, chart_of_accounts, linked_item
):
    transaction = await _one_transaction(session, business)
    await apply_category(
        session,
        transaction=transaction,
        category_id=chart_of_accounts["Travel"].id,
        actor="agent:categorizer-v1",
        confidence=0.97,
    )
    assert transaction.needs_review is False
    assert transaction.last_reviewed_at is None


async def test_human_review_stamps_last_reviewed_at(
    session, business, chart_of_accounts, linked_item
):
    transaction = await _one_transaction(session, business)
    await apply_category(
        session,
        transaction=transaction,
        category_id=chart_of_accounts["Travel"].id,
        actor="agent:categorizer-v1",
        confidence=0.2,
    )
    await confirm_category(session, transaction=transaction, actor="cli:hakeem")

    assert transaction.needs_review is False
    assert transaction.last_reviewed_at is not None


async def test_category_from_another_business_is_refused(session, business, linked_item):
    other_business = await repo.create_business(
        session, tenant_id=business.tenant_id, name="Other LLC"
    )
    foreign = await repo.create_category(
        session, business=other_business, name="Rent", account_type="expense"
    )
    transaction = await _one_transaction(session, business)

    with pytest.raises(ValidationError, match="different business"):
        await apply_category(
            session,
            transaction=transaction,
            category_id=foreign.id,
            actor="cli:hakeem",
            confidence=1.0,
        )


async def test_confirming_an_uncategorized_transaction_is_refused(session, business, linked_item):
    transaction = await _one_transaction(session, business)
    with pytest.raises(ValidationError, match="no category"):
        await confirm_category(session, transaction=transaction, actor="cli:hakeem")


# --- protecting a human review from being overwritten -------------------


async def test_a_non_reviewing_write_is_refused_once_a_human_reviewed(
    session, business, chart_of_accounts, linked_item
):
    """The core guard: an agent-style write must never clobber a human's call."""
    transaction = await _one_transaction(session, business)
    await confirm_category_after_setting(
        session, transaction, chart_of_accounts["Payroll"].id, actor="cli:hakeem"
    )

    with pytest.raises(ValidationError, match="reviewed by a human"):
        await apply_category(
            session,
            transaction=transaction,
            category_id=chart_of_accounts["Travel"].id,
            actor="agent:categorizer-v1",
            confidence=0.9,
        )
    # The refusal must not have touched anything.
    assert transaction.category_id == chart_of_accounts["Payroll"].id


async def test_force_overrides_the_review_protection_deliberately(
    session, business, chart_of_accounts, linked_item
):
    transaction = await _one_transaction(session, business)
    await confirm_category_after_setting(
        session, transaction, chart_of_accounts["Payroll"].id, actor="cli:hakeem"
    )

    await apply_category(
        session,
        transaction=transaction,
        category_id=chart_of_accounts["Travel"].id,
        actor="agent:categorizer-v1",
        confidence=0.9,
        force=True,
    )
    assert transaction.category_id == chart_of_accounts["Travel"].id


async def test_a_human_write_never_needs_force_even_after_a_prior_review(
    session, business, chart_of_accounts, linked_item
):
    """mark_reviewed=True is itself the override — a human correcting their
    own earlier review must never need `force` too."""
    transaction = await _one_transaction(session, business)
    await confirm_category_after_setting(
        session, transaction, chart_of_accounts["Payroll"].id, actor="cli:hakeem"
    )

    await apply_category(
        session,
        transaction=transaction,
        category_id=chart_of_accounts["Travel"].id,
        actor="cli:hakeem",
        confidence=1.0,
        mark_reviewed=True,
    )
    assert transaction.category_id == chart_of_accounts["Travel"].id


async def confirm_category_after_setting(session, transaction, category_id, *, actor):
    """Helper: get a transaction into a real "reviewed" state for these tests."""
    await apply_category(
        session,
        transaction=transaction,
        category_id=category_id,
        actor=actor,
        confidence=1.0,
        mark_reviewed=True,
    )


# --- the agent graph ----------------------------------------------------


async def test_agent_categorizes_and_records_its_rationale(
    session, business, chart_of_accounts, linked_item
):
    transaction = await _one_transaction(session, business)
    outcome = await categorize_transaction(
        session,
        transaction_id=transaction.id,
        classifier=classifier_returning("Software & Subscriptions", 0.93, "AWS is cloud hosting"),
    )

    assert outcome.source == "model"
    assert outcome.category_name == "Software & Subscriptions"
    assert outcome.needs_review is False
    history = await repo.list_history(session, transaction_id=transaction.id)
    assert history[0].actor == "agent:categorizer-v1"
    assert history[0].rationale == "AWS is cloud hosting"


async def test_agent_refuses_a_category_outside_the_chart_of_accounts(
    session, business, chart_of_accounts, linked_item
):
    transaction = await _one_transaction(session, business)
    outcome = await categorize_transaction(
        session,
        transaction_id=transaction.id,
        classifier=classifier_returning("Interdimensional Freight", 0.99),
    )

    assert outcome.source == "invalid_category"
    assert outcome.category_id is None
    assert outcome.needs_review is True


async def test_a_standing_rule_beats_the_model(session, business, chart_of_accounts, linked_item):
    await repo.create_rule(
        session,
        business=business,
        match_type="vendor_equals",
        pattern="AWS",
        category_id=chart_of_accounts["Software & Subscriptions"].id,
        note="Always infra",
        created_by="cli:hakeem",
    )
    transaction = await _one_transaction(session, business)

    async def never_called(system: str, prompt: str) -> Classification:
        raise AssertionError("The model must not be consulted when a rule matches")

    outcome = await categorize_transaction(
        session, transaction_id=transaction.id, classifier=never_called
    )

    assert outcome.source == "rule"
    assert outcome.category_name == "Software & Subscriptions"
    assert outcome.confidence == 1.0
    assert outcome.needs_review is False


async def test_no_chart_of_accounts_means_review_not_a_guess(session, business, linked_item):
    transaction = await _one_transaction(session, business)
    outcome = await categorize_transaction(
        session, transaction_id=transaction.id, classifier=classifier_returning("Anything")
    )
    assert outcome.source == "no_categories"
    assert outcome.category_id is None
    assert outcome.needs_review is True


async def test_prompt_includes_the_chart_of_accounts_and_reviewed_precedents(
    session, business, chart_of_accounts, linked_item
):
    await sync_item(session, item=linked_item)
    transactions = await repo.list_transactions(
        session, repo.TransactionFilters(business_id=business.id, search="aws", limit=5)
    )
    first = transactions[0]
    await apply_category(
        session,
        transaction=first,
        category_id=chart_of_accounts["Software & Subscriptions"].id,
        actor="cli:hakeem",
        confidence=1.0,
        mark_reviewed=True,
    )

    later = await _one_transaction(session, business, vendor="AWS")
    classifier = classifier_returning("Software & Subscriptions")
    await categorize_transaction(session, transaction_id=later.id, classifier=classifier)

    prompt = classifier.last_prompt  # type: ignore[attr-defined]
    assert "Coding Crafts" in prompt
    assert "Software & Subscriptions" in prompt
    assert "human-reviewed" in prompt


# --- accounting semantics reach the agent -------------------------------


async def test_outcome_reports_the_account_type(session, business, chart_of_accounts, linked_item):
    transaction = await _one_transaction(session, business)
    outcome = await categorize_transaction(
        session,
        transaction_id=transaction.id,
        classifier=classifier_returning("Software & Subscriptions", 0.93),
    )
    from books.core.accounting import AccountType

    assert outcome.account_type is AccountType.EXPENSE


async def test_prompt_states_the_account_type_and_entry_side(
    session, business, chart_of_accounts, linked_item
):
    """The model cannot reason about debits without being told which way it goes."""
    spend = await _one_transaction(session, business, vendor="AWS")
    classifier = classifier_returning("Software & Subscriptions")
    await categorize_transaction(session, transaction_id=spend.id, classifier=classifier)

    prompt = classifier.last_prompt  # type: ignore[attr-defined]
    assert "[expense]" in prompt
    assert "[revenue]" in prompt
    assert "DEBIT the category" in prompt
    assert "money out" in prompt


async def test_prompt_flips_the_entry_side_for_money_in(
    session, business, chart_of_accounts, linked_item
):
    account = (await repo.list_accounts(session, business_id=business.id))[0]
    transaction_id, _ = await repo.upsert_transaction(
        session,
        values={
            "tenant_id": business.tenant_id,
            "business_id": business.id,
            "account_id": account.id,
            "provider_transaction_id": "income-1",
            "amount": Decimal("25000.00"),
            "date": date(2026, 2, 16),
            "vendor": "Acme Corp",
        },
    )
    await session.flush()

    classifier = classifier_returning("Consulting Revenue")
    await categorize_transaction(session, transaction_id=transaction_id, classifier=classifier)

    prompt = classifier.last_prompt  # type: ignore[attr-defined]
    assert "CREDIT the category" in prompt
    assert "money in" in prompt


async def test_a_refund_can_be_credited_to_the_expense_it_came_from(
    session, business, chart_of_accounts, linked_item
):
    """Money in is not always revenue — the model may pick an expense."""
    account = (await repo.list_accounts(session, business_id=business.id))[0]
    transaction_id, _ = await repo.upsert_transaction(
        session,
        values={
            "tenant_id": business.tenant_id,
            "business_id": business.id,
            "account_id": account.id,
            "provider_transaction_id": "refund-1",
            "amount": Decimal("412.55"),
            "date": date(2026, 2, 20),
            "vendor": "AWS",
            "description": "AWS credit memo",
        },
    )
    await session.flush()

    outcome = await categorize_transaction(
        session,
        transaction_id=transaction_id,
        classifier=classifier_returning("Software & Subscriptions", 0.9, "Refund of an AWS charge"),
    )

    from books.core.accounting import AccountType, EntrySide, increases_balance

    transaction = await repo.get_transaction(session, transaction_id)
    assert outcome.account_type is AccountType.EXPENSE
    assert transaction.entry_side == EntrySide.CREDIT
    # A credit to an expense reduces it — that is what makes this a refund.
    assert increases_balance(outcome.account_type, transaction.entry_side) is False
