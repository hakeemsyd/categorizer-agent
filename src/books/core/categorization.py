"""The one and only write path for a transaction's category.

Agent, CLI, MCP and any future face all land here (plan.md §2, §6). Every call
appends to ``categorization_history`` instead of silently overwriting, and
``last_reviewed_at`` moves only when a human explicitly reviews — never when
the agent writes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from books.config import get_settings
from books.core import repository as repo
from books.core.errors import ValidationError
from books.core.models import CategorizationHistory, Rule, Transaction
from books.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class CategoryDecision:
    """What some actor decided, before it is written."""

    category_id: uuid.UUID | None
    confidence: float | None
    rationale: str | None
    actor: str


async def apply_category(
    session: AsyncSession,
    *,
    transaction: Transaction,
    category_id: uuid.UUID | None,
    actor: str,
    confidence: float | None = None,
    rationale: str | None = None,
    mark_reviewed: bool = False,
    threshold: float | None = None,
    force: bool = False,
) -> Transaction:
    """Set a transaction's category and record who did it and why.

    ``mark_reviewed`` is what separates a human confirmation from an agent
    write: only it touches ``last_reviewed_at`` and clears ``needs_review``
    unconditionally.

    A write that is not itself a human review (``mark_reviewed=False``) is
    refused outright if a human already reviewed this transaction — the
    agent re-running (on one transaction or across a whole business) must
    never silently replace a human's decision. Pass ``force=True`` to
    override deliberately. Human-driven writes always pass
    ``mark_reviewed=True`` and never hit this guard.
    """
    if not mark_reviewed and not force and transaction.last_reviewed_at is not None:
        raise ValidationError(
            f"Transaction {transaction.id} was reviewed by a human on "
            f"{transaction.last_reviewed_at:%Y-%m-%d} — refusing to overwrite it. "
            "Pass force=True to override deliberately."
        )

    if category_id is not None:
        category = await repo.get_category(session, category_id)
        if category.business_id != transaction.business_id:
            raise ValidationError("Category belongs to a different business than the transaction")
        if category.archived:
            raise ValidationError(f"Category {category.name!r} is archived")

    cutoff = get_settings().confidence_threshold if threshold is None else threshold
    transaction.category_id = category_id
    transaction.confidence = None if confidence is None else Decimal(str(round(confidence, 3)))

    if mark_reviewed:
        transaction.needs_review = False
        transaction.last_reviewed_at = datetime.now(UTC)
    else:
        transaction.needs_review = category_id is None or (confidence or 0.0) < cutoff

    session.add(
        CategorizationHistory(
            tenant_id=transaction.tenant_id,
            transaction_id=transaction.id,
            actor=actor,
            category_id=category_id,
            confidence=transaction.confidence,
            rationale=rationale,
        )
    )
    await session.flush()

    log.info(
        "category.applied",
        transaction_id=str(transaction.id),
        category_id=str(category_id) if category_id else None,
        actor=actor,
        confidence=confidence,
        needs_review=transaction.needs_review,
        reviewed=mark_reviewed,
    )
    return transaction


async def confirm_category(
    session: AsyncSession, *, transaction: Transaction, actor: str, note: str | None = None
) -> Transaction:
    """A human agrees with whatever is already there."""
    if transaction.category_id is None:
        raise ValidationError("Cannot confirm a transaction that has no category yet")
    return await apply_category(
        session,
        transaction=transaction,
        category_id=transaction.category_id,
        actor=actor,
        confidence=1.0,
        rationale=note or "Confirmed by human review",
        mark_reviewed=True,
    )


def match_rule(transaction: Transaction, rules: list[Rule]) -> Rule | None:
    """First matching standing rule, by priority.

    Rules are deterministic human instructions, so a hit short-circuits the
    model entirely — cheaper, and it makes corrections stick.
    """
    vendor = (transaction.vendor or "").lower()
    description = (transaction.description or "").lower()
    for rule in sorted(rules, key=lambda r: (r.priority, r.created_at)):
        pattern = rule.pattern.lower()
        if rule.match_type == "vendor_equals" and vendor == pattern:
            return rule
        if rule.match_type == "vendor_contains" and pattern in vendor:
            return rule
        if rule.match_type == "description_contains" and pattern in description:
            return rule
    return None
