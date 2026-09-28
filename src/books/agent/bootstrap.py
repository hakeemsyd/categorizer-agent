"""Cold start: categorizing a fresh import when there is no history at all.

The per-transaction path has nothing to work with on day one — no precedents,
no corrections, no rules. Asking the model the same question once per row is
slow, costs more, and is *inconsistent*: two transactions from the same
merchant are separate calls and can land on different categories.

So the first pass runs per merchant instead. Each distinct merchant gets one
decision, informed by a few representative transactions, and that decision is
written to every transaction from it. On the sample data that is 95 calls
instead of 222, and the ratio only improves as history grows.

What this deliberately does *not* do is mark anything reviewed. A bootstrap is
still the model's opinion; it just arrives organized by merchant, which is
also the unit a human wants to review in (`books tx review`), and the unit a
correction propagates across (`categorization.propagate_correction`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from books.agent.categorizer import Classification, Classifier, _lazy_anthropic_classifier
from books.agent.prompts import BOOTSTRAP_SYSTEM_PROMPT, VENDOR_TEMPLATE
from books.config import get_settings
from books.core import repository as repo
from books.core.categorization import apply_category
from books.core.models import Category, Rule
from books.logging import get_logger

log = get_logger(__name__)


@dataclass
class VendorDecision:
    vendor_label: str
    category_name: str | None
    confidence: float
    rationale: str
    applied: int = 0
    error: str | None = None


@dataclass
class BootstrapResult:
    vendors_seen: int = 0
    vendors_decided: int = 0
    transactions_categorized: int = 0
    skipped_no_merchant: int = 0
    decisions: list[VendorDecision] = field(default_factory=list)


async def bootstrap_business(
    session: AsyncSession,
    *,
    business_id: uuid.UUID,
    classifier: Classifier | None = None,
    max_vendors: int | None = None,
) -> BootstrapResult:
    """Categorize every uncategorized transaction, one decision per merchant."""
    settings = get_settings()
    business = await repo.get_business(session, business_id)
    categories = list(await repo.list_categories(session, business_id=business_id))
    if not categories:
        raise_no_chart(business.name)

    groups = await repo.group_uncategorized_by_vendor(
        session, business_id=business_id, sample_size=settings.vendor_sample_size
    )
    if max_vendors is not None:
        groups = groups[:max_vendors]

    decide = classifier or _lazy_anthropic_classifier
    rules = list(await repo.list_rules(session, business_id=business_id))
    result = BootstrapResult(vendors_seen=len(groups))

    for group in groups:
        decision = await _decide_vendor(
            session,
            business_name=business.name,
            business_details=business.details,
            categories=categories,
            group=group,
            rules=rules,
            decide=decide,
        )
        result.decisions.append(decision)
        if decision.category_name is None:
            continue

        result.vendors_decided += 1
        result.transactions_categorized += decision.applied

    log.info(
        "bootstrap.completed",
        business_id=str(business_id),
        vendors=result.vendors_seen,
        decided=result.vendors_decided,
        transactions=result.transactions_categorized,
    )
    return result


async def _decide_vendor(
    session: AsyncSession,
    *,
    business_name: str,
    business_details: str | None,
    categories: list[Category],
    group: repo.VendorGroup,
    rules: list[Rule],
    decide: Classifier,
) -> VendorDecision:
    """One model call for one merchant, written to all of its transactions."""
    from books.core.categorization import match_rule

    settings = get_settings()

    # A standing rule already answers this merchant — don't pay for a model
    # call to be told something a human already decided.
    rule = match_rule(group.sample[0], rules) if group.sample else None
    if rule is not None:
        category = await repo.get_category(session, rule.category_id)
        applied = await _apply_to_group(
            session,
            group=group,
            category=category,
            confidence=1.0,
            rationale=f"Standing rule: {rule.match_type}={rule.pattern!r}",
            actor=settings.categorizer_actor,
        )
        return VendorDecision(
            vendor_label=group.label,
            category_name=category.name,
            confidence=1.0,
            rationale="matched a standing rule",
            applied=applied,
        )

    prompt = VENDOR_TEMPLATE.format(
        business_name=business_name,
        business_details=business_details or "",
        categories="\n".join(
            f"- {c.name} [{c.account_type.value}]" + (f": {c.description}" if c.description else "")
            for c in categories
        ),
        vendor=group.label,
        transaction_count=group.count,
        samples="\n".join(
            f"- {t.date} {t.amount:,.2f} "
            f"({'money out' if t.entry_side == 'debit' else 'money in'}) "
            f"{t.description or ''}".rstrip()
            for t in group.sample
        ),
    )

    try:
        classification: Classification = await decide(BOOTSTRAP_SYSTEM_PROMPT, prompt)
    except Exception as exc:  # one bad merchant must not sink the whole pass
        log.warning("bootstrap.vendor_failed", vendor=group.vendor_label, error=str(exc))
        return VendorDecision(
            vendor_label=group.label,
            category_name=None,
            confidence=0.0,
            rationale="",
            error=str(exc),
        )

    matched = await repo.find_category_by_name(
        session, business_id=group.sample[0].business_id, name=classification.category_name
    )
    if matched is None:
        # Outside the chart of accounts — refuse rather than invent, same as
        # the per-transaction path. These stay uncategorized for review.
        log.warning(
            "bootstrap.invalid_category",
            vendor=group.vendor_label,
            proposed=classification.category_name,
        )
        return VendorDecision(
            vendor_label=group.label,
            category_name=None,
            confidence=0.0,
            rationale=f"proposed {classification.category_name!r}, not in the chart of accounts",
            error="invalid_category",
        )

    applied = await _apply_to_group(
        session,
        group=group,
        category=matched,
        confidence=classification.confidence,
        rationale=classification.rationale,
        actor=settings.categorizer_actor,
    )
    return VendorDecision(
        vendor_label=group.label,
        category_name=matched.name,
        confidence=classification.confidence,
        rationale=classification.rationale,
        applied=applied,
    )


async def _apply_to_group(
    session: AsyncSession,
    *,
    group: repo.VendorGroup,
    category: Category,
    confidence: float,
    rationale: str,
    actor: str,
) -> int:
    """Write one merchant's decision to each of its transactions.

    Goes through ``apply_category`` per row rather than a bulk UPDATE: the
    audit trail and the review-protection guard are the point, and a merchant
    group is small.
    """
    applied = 0
    for transaction_id in group.transaction_ids:
        transaction = await repo.get_transaction(session, transaction_id)
        if transaction.last_reviewed_at is not None:
            continue  # a human already ruled here; leave it
        await apply_category(
            session,
            transaction=transaction,
            category_id=category.id,
            actor=actor,
            confidence=confidence,
            rationale=f"{rationale} (decided once for {group.label})",
            mark_reviewed=False,
        )
        applied += 1
    return applied


def raise_no_chart(business_name: str) -> None:
    from books.core.errors import ValidationError

    raise ValidationError(
        f"{business_name} has no chart of accounts yet, so there is nothing for the "
        "agent to choose from. Run `books category bootstrap` to build one from the "
        "transactions already synced, or `books category seed` for a generic chart."
    )
