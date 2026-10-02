"""Step 1 of a cold start: build the chart of accounts from what actually landed.

Categorizing into a chart that doesn't fit the business is the wrong problem
solved well. The generic chart (`books category seed`) is a reasonable default
when there is no data yet, but once transactions are synced there is something
much better to work from: the real merchant landscape. A consultancy paying
AWS, Gusto and a landlord needs different buckets from a shop buying stock.

So this reads the transactions, shows the model what the business actually
spends on and earns from, and proposes the accounts needed to cover it. A
human approves before anything is created — a chart of accounts is a
structural decision, not something to have appear silently.

Deliberately additive: it proposes what's *missing*, never renames or deletes
what a human already set up.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Protocol

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from books.agent.categorizer import build_chat_model
from books.agent.prompts import CHART_SYSTEM_PROMPT, CHART_TEMPLATE
from books.core import repository as repo
from books.core.accounting import AccountType
from books.core.chart_of_accounts import DEFAULT_CHART
from books.core.errors import ValidationError
from books.core.models import Business, Category
from books.logging import get_logger

log = get_logger(__name__)

# A chart is a human-readable structure. Past a certain size it stops being
# one, and almost certainly means the model split hairs it should not have.
MAX_PROPOSED = 60


class ProposedCategory(BaseModel):
    """One account the model thinks this business needs."""

    name: str = Field(description="Short account name, title case")
    account_type: AccountType
    description: str = Field(description="What belongs here — read by the categorizer later")
    covers: list[str] = Field(
        default_factory=list,
        description="A few merchants from this business this account is for",
    )


class ChartProposal(BaseModel):
    categories: list[ProposedCategory]

    @field_validator("categories", mode="before")
    @classmethod
    def _accept_json_string(cls, value: object) -> object:
        """Unwrap a proposal the model handed back as a JSON string.

        Structured output usually arrives as real arguments, but on a long
        answer the model sometimes serializes the whole thing and puts it in
        this one field — occasionally the entire ``{"categories": [...]}``
        object. Seen live on a 123-merchant business. Failing the run over a
        wrapper is the wrong call when the content is right there.
        """
        if not isinstance(value, str):
            return value
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value
        if isinstance(parsed, dict):
            return parsed.get("categories", parsed)
        return parsed


class ChartProposer(Protocol):
    """Anything that can turn the merchant landscape into a proposed chart.

    Injectable so tests exercise the whole flow without a model call.
    """

    async def __call__(self, system: str, prompt: str) -> ChartProposal: ...


@lru_cache(maxsize=1)
def configured_chart_proposer() -> ChartProposer:
    model = build_chat_model().with_structured_output(ChartProposal)

    async def propose(system: str, prompt: str) -> ChartProposal:
        result = await model.ainvoke(
            [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        )
        if isinstance(result, ChartProposal):
            return result
        return ChartProposal.model_validate(result)

    return propose


async def _lazy_proposer(system: str, prompt: str) -> ChartProposal:
    return await configured_chart_proposer()(system, prompt)


@dataclass
class ChartResult:
    proposed: list[ProposedCategory] = field(default_factory=list)
    created: list[Category] = field(default_factory=list)
    skipped_existing: list[str] = field(default_factory=list)
    merchants_seen: int = 0

    @property
    def count(self) -> int:
        return len(self.created)


async def propose_chart(
    session: AsyncSession,
    *,
    business_id: uuid.UUID,
    proposer: ChartProposer | None = None,
    max_merchants: int = 120,
) -> tuple[Business, list[ProposedCategory], int]:
    """Ask the model what accounts this business's transactions need.

    Proposes only; nothing is written. Returns the business, the proposal, and
    how many merchant groups it was based on.
    """
    business = await repo.get_business(session, business_id)
    groups = await repo.group_uncategorized_by_vendor(
        session, business_id=business_id, sample_size=1, only_uncategorized=False
    )
    if not groups:
        raise ValidationError(
            f"{business.name} has no transactions with an identifiable merchant yet. "
            "Sync first, or use `books category seed` for a generic starting chart."
        )

    existing = list(await repo.list_categories(session, business_id=business_id))
    prompt = CHART_TEMPLATE.format(
        business_name=business.name,
        business_details=business.details or "(no description given)",
        existing=_render_existing(existing),
        reference="\n".join(
            f"- {entry.name} [{entry.account_type.value}]" for entry in DEFAULT_CHART
        ),
        merchants=_render_merchants(groups[:max_merchants]),
        merchant_count=len(groups),
    )

    decide = proposer or _lazy_proposer
    proposal = await decide(CHART_SYSTEM_PROMPT, prompt)
    categories = _clean(proposal, existing)

    log.info(
        "chart.proposed",
        business_id=str(business_id),
        merchants=len(groups),
        proposed=len(categories),
    )
    return business, categories, len(groups)


async def apply_proposal(
    session: AsyncSession, *, business: Business, proposed: list[ProposedCategory]
) -> ChartResult:
    """Create the approved accounts. Existing names are left untouched."""
    result = ChartResult(proposed=proposed)
    for candidate in proposed:
        if await repo.find_category_by_name(session, business_id=business.id, name=candidate.name):
            result.skipped_existing.append(candidate.name)
            continue
        result.created.append(
            await repo.create_category(
                session,
                business=business,
                name=candidate.name,
                account_type=candidate.account_type,
                description=candidate.description,
            )
        )
    log.info(
        "chart.applied",
        business_id=str(business.id),
        created=len(result.created),
        skipped=len(result.skipped_existing),
    )
    return result


async def build_chart(
    session: AsyncSession,
    *,
    business_id: uuid.UUID,
    proposer: ChartProposer | None = None,
    apply: bool = False,
    max_merchants: int = 120,
) -> ChartResult:
    """Propose a chart, and create it only when asked.

    One entry point for every face, because the two halves must not drift: a
    preview that shows something other than what approval would create is worse
    than no preview. With ``apply=False`` nothing is written.
    """
    business, proposed, seen = await propose_chart(
        session, business_id=business_id, proposer=proposer, max_merchants=max_merchants
    )
    if not apply:
        return ChartResult(proposed=proposed, merchants_seen=seen)
    result = await apply_proposal(session, business=business, proposed=proposed)
    result.merchants_seen = seen
    return result


def _clean(proposal: ChartProposal, existing: list[Category]) -> list[ProposedCategory]:
    """Drop duplicates and anything the business already has.

    The model is asked for gaps, but asking is not enforcing — a name it
    repeats, or one that already exists under different casing, would
    otherwise become a confusing near-duplicate account.
    """
    taken = {c.name.strip().lower() for c in existing}
    cleaned: list[ProposedCategory] = []
    for candidate in proposal.categories:
        name = candidate.name.strip()
        if not name or name.lower() in taken:
            continue
        taken.add(name.lower())
        cleaned.append(candidate.model_copy(update={"name": name}))
    return cleaned[:MAX_PROPOSED]


def _render_existing(existing: list[Category]) -> str:
    if not existing:
        return "(none yet — this business has no chart of accounts)"
    return "\n".join(f"- {c.name} [{c.account_type.value}]" for c in existing)


def _render_merchants(groups: list[repo.VendorGroup]) -> str:
    """Split by direction, because the direction is what gets mistyped.

    With one flat list the model reads a row of familiar retailers and proposes
    a revenue account for them, even though every one of those lines is money
    leaving the business. Two headed sections make that error require ignoring
    a heading rather than overlooking a field at the end of a line.
    """
    out = [g for g in groups if g.direction != "credit"]
    incoming = [g for g in groups if g.direction == "credit"]
    sections = []
    for title, subset, gloss in (
        ("MONEY OUT", out, "the business paid these — never revenue"),
        ("MONEY IN", incoming, "these paid the business — never an expense"),
    ):
        if not subset:
            continue
        body = "\n".join(_merchant_line(g) for g in subset)
        sections.append(f"{title} ({gloss}):\n{body}")
    return "\n\n".join(sections)


def _merchant_line(group: repo.VendorGroup) -> str:
    detail = f" — {group.description_label.title()}" if group.description_label else ""
    return (
        f"- {group.vendor_label}{detail} | {group.count}x | "
        f"{abs(group.smallest):,.2f}-{abs(group.largest):,.2f}"
    )
