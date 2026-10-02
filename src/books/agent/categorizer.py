"""LangGraph categorization subgraph (plan.md §6).

Flow: load context -> standing rules -> model -> validate against the chart of
accounts -> persist via ``apply_category``. The write is delegated, never
duplicated: the agent has no privileged path to ``transactions.category_id``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from typing import Any, Protocol, TypedDict

from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from books.agent.prompts import (
    NO_CORRECTIONS,
    NO_SIMILAR,
    SYSTEM_PROMPT,
    TRANSACTION_TEMPLATE,
)
from books.config import get_settings
from books.core import repository as repo
from books.core.accounting import AccountType, EntrySide
from books.core.categorization import apply_category, match_rule
from books.core.errors import ConfigurationError
from books.core.models import Account, Business, Category, Transaction
from books.logging import get_logger

log = get_logger(__name__)


class Classification(BaseModel):
    """What the model is asked to return."""

    category_name: str = Field(description="Exact category name from the chart of accounts")
    confidence: float = Field(ge=0.0, le=1.0, description="Calibrated confidence in the choice")
    rationale: str = Field(description="One or two sentences citing the evidence used")


class Classifier(Protocol):
    """Anything that can turn a prompt into a :class:`Classification`.

    Injectable so tests exercise the whole graph without a model call.
    """

    async def __call__(self, system: str, prompt: str) -> Classification: ...


@dataclass
class CategorizationOutcome:
    transaction_id: uuid.UUID
    category_id: uuid.UUID | None
    category_name: str | None
    account_type: AccountType | None
    confidence: float
    needs_review: bool
    rationale: str
    source: str  # 'rule' | 'model' | 'no_categories' | 'invalid_category' | 'error'


class _State(TypedDict, total=False):
    session: AsyncSession
    classifier: Classifier
    force: bool
    transaction: Transaction
    business: Business
    account: Account
    categories: list[Category]
    similar: list[Transaction]
    corrections: list[tuple[Transaction, uuid.UUID]]
    prompt: str
    classification: Classification | None
    category_id: uuid.UUID | None
    category_name: str | None
    account_type: AccountType | None
    confidence: float
    rationale: str
    source: str


# --- nodes --------------------------------------------------------------


async def _load_context(state: _State) -> dict[str, Any]:
    session, transaction = state["session"], state["transaction"]
    settings = get_settings()
    categories = list(await repo.list_categories(session, business_id=transaction.business_id))
    similar = list(
        await repo.find_similar_transactions(
            session, transaction=transaction, limit=settings.similar_transaction_limit
        )
    )
    corrections = list(
        await repo.find_vendor_corrections(
            session, transaction=transaction, limit=settings.correction_example_limit
        )
    )
    return {
        "categories": categories,
        "similar": similar,
        "corrections": corrections,
        "business": await repo.get_business(session, transaction.business_id),
        "account": await session.get(Account, transaction.account_id),
    }


async def _apply_rules(state: _State) -> dict[str, Any]:
    session, transaction = state["session"], state["transaction"]
    rules = list(await repo.list_rules(session, business_id=transaction.business_id))
    rule = match_rule(transaction, rules)
    if rule is None:
        return {"source": ""}
    category = await repo.get_category(session, rule.category_id)
    return {
        "category_id": category.id,
        "category_name": category.name,
        "account_type": category.account_type,
        "confidence": 1.0,
        "rationale": f"Standing rule: {rule.match_type}={rule.pattern!r}"
        + (f" — {rule.note}" if rule.note else ""),
        "source": "rule",
    }


async def _classify(state: _State) -> dict[str, Any]:
    if not state["categories"]:
        return {
            "category_id": None,
            "category_name": None,
            "confidence": 0.0,
            "rationale": "Business has no chart of accounts yet; cannot categorize.",
            "source": "no_categories",
        }

    prompt = _render_prompt(state)
    classification = await state["classifier"](SYSTEM_PROMPT, prompt)
    return {"prompt": prompt, "classification": classification}


async def _validate(state: _State) -> dict[str, Any]:
    classification = state.get("classification")
    if classification is None:
        return {}

    session, transaction = state["session"], state["transaction"]
    category = await repo.find_category_by_name(
        session, business_id=transaction.business_id, name=classification.category_name
    )
    if category is None:
        # The model named something outside the chart of accounts. Refuse the
        # write rather than inventing a category, and route to human review.
        log.warning(
            "categorize.invalid_category",
            transaction_id=str(transaction.id),
            proposed=classification.category_name,
        )
        return {
            "category_id": None,
            "category_name": None,
            "confidence": 0.0,
            "rationale": (
                f"Model proposed {classification.category_name!r}, which is not in this "
                f"business's chart of accounts. Left uncategorized for review."
            ),
            "source": "invalid_category",
        }
    return {
        "category_id": category.id,
        "category_name": category.name,
        "account_type": category.account_type,
        "confidence": classification.confidence,
        "rationale": classification.rationale,
        "source": "model",
    }


async def _persist(state: _State) -> dict[str, Any]:
    session, transaction = state["session"], state["transaction"]
    await apply_category(
        session,
        transaction=transaction,
        category_id=state.get("category_id"),
        actor=get_settings().categorizer_actor,
        confidence=state.get("confidence", 0.0),
        rationale=state.get("rationale"),
        mark_reviewed=False,  # the agent never marks a transaction human-reviewed
        force=state.get("force", False),
    )
    return {}


def _route_after_rules(state: _State) -> str:
    return "persist" if state.get("source") == "rule" else "classify"


# --- graph --------------------------------------------------------------


def build_graph() -> Any:
    graph = StateGraph(_State)
    graph.add_node("load_context", _load_context)
    graph.add_node("apply_rules", _apply_rules)
    graph.add_node("classify", _classify)
    graph.add_node("validate", _validate)
    graph.add_node("persist", _persist)

    graph.set_entry_point("load_context")
    graph.add_edge("load_context", "apply_rules")
    graph.add_conditional_edges(
        "apply_rules", _route_after_rules, {"classify": "classify", "persist": "persist"}
    )
    graph.add_edge("classify", "validate")
    graph.add_edge("validate", "persist")
    graph.add_edge("persist", END)
    return graph.compile()


_COMPILED: Any | None = None


def _compiled() -> Any:
    global _COMPILED
    if _COMPILED is None:
        _COMPILED = build_graph()
    return _COMPILED


async def categorize_transaction(
    session: AsyncSession,
    *,
    transaction_id: uuid.UUID,
    classifier: Classifier | None = None,
    force: bool = False,
) -> CategorizationOutcome:
    """Categorize one transaction and write the result.

    Pass ``classifier`` to substitute the model (tests, replays, evals).
    Raises :class:`ValidationError` if a human already reviewed this
    transaction, unless ``force=True`` — see ``apply_category``.
    """
    transaction = await repo.get_transaction(session, transaction_id)
    state: dict[str, Any] = {
        "session": session,
        "transaction": transaction,
        # Lazy: a standing rule short-circuits before the model is ever built,
        # so a business running purely on rules needs no Anthropic key.
        "classifier": classifier or _lazy_classifier,
        "force": force,
        "confidence": 0.0,
        "rationale": "",
        "source": "",
    }
    final = await _compiled().ainvoke(state)
    return CategorizationOutcome(
        transaction_id=transaction.id,
        category_id=transaction.category_id,
        category_name=final.get("category_name"),
        account_type=final.get("account_type"),
        confidence=float(transaction.confidence or 0),
        needs_review=transaction.needs_review,
        rationale=final.get("rationale") or "",
        source=final.get("source") or "model",
    )


# --- the real model -----------------------------------------------------


async def _lazy_classifier(system: str, prompt: str) -> Classification:
    return await configured_classifier()(system, prompt)


@lru_cache(maxsize=1)
def build_chat_model() -> Any:
    """The configured chat model, shared by every agent that needs one.

    One place, because the awkward per-provider details — a workspace header,
    a temperature the model rejects, an OpenAI-compatible base URL — are the
    kind of thing that silently diverges between call sites otherwise.
    """
    settings = get_settings()
    if settings.llm_provider == "anthropic":
        return _build_anthropic(settings)
    return _build_qwen(settings)


def _build_qwen(settings: Any) -> Any:
    """Qwen through DashScope, which serves the OpenAI wire format.

    So the OpenAI client is the client here — there is no Qwen-specific SDK
    worth taking on for a chat-completions call.
    """
    if not settings.qwen_api_key:
        raise ConfigurationError(
            "BOOKS_QWEN_API_KEY is unset — the agent cannot call the model. "
            "Get a key from Alibaba Cloud Model Studio, and make sure it is "
            "issued for the same region as BOOKS_QWEN_BASE_URL."
        )

    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=settings.categorizer_model,
        api_key=SecretStr(settings.qwen_api_key),
        base_url=settings.qwen_base_url,
        # Low rather than absent: unlike the current Claude models Qwen still
        # accepts temperature, and every task here picks one item from a fixed
        # list, where sampling variety is a liability rather than a feature.
        temperature=0,
        max_completion_tokens=4096,
        timeout=120,
    )


def _build_anthropic(settings: Any) -> Any:
    if not settings.anthropic_api_key:
        raise ConfigurationError(
            "BOOKS_ANTHROPIC_API_KEY is unset but BOOKS_LLM_PROVIDER=anthropic."
        )

    from langchain_anthropic import ChatAnthropic

    # Only set for an organization-level key, which Anthropic otherwise
    # rejects with "not scoped to a workspace" — see BOOKS_ANTHROPIC_WORKSPACE_ID.
    headers = (
        {"anthropic-workspace-id": settings.anthropic_workspace_id}
        if settings.anthropic_workspace_id
        else None
    )
    return ChatAnthropic(
        model_name=settings.categorizer_model,
        api_key=SecretStr(settings.anthropic_api_key),
        # No temperature: the current Claude models reject it as deprecated.
        max_tokens_to_sample=4096,
        timeout=120,
        stop=None,
        default_headers=headers,
    )


@lru_cache(maxsize=1)
def configured_classifier() -> Classifier:
    """Claude-backed classifier, built on first use so tests never need a key."""
    model = build_chat_model().with_structured_output(Classification)

    async def classify(system: str, prompt: str) -> Classification:
        result = await model.ainvoke(
            [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        )
        if isinstance(result, Classification):
            return result
        return Classification.model_validate(result)

    return classify


# --- prompt rendering ---------------------------------------------------


def _render_prompt(state: _State) -> str:
    transaction = state["transaction"]
    business = state["business"]
    account = state.get("account")
    # The account type is part of the choice, not decoration: it is what tells
    # the model a refund belongs on an expense rather than on revenue.
    categories = "\n".join(
        f"- {c.name} [{c.account_type.value}]" + (f": {c.description}" if c.description else "")
        for c in state["categories"]
    )
    categories_list = state["categories"]
    # Spell out which rows carry a human's authority and which are only the
    # model's own past guesses — the system prompt ranks them very differently.
    similar = (
        "\n".join(
            f"- {t.date} {_money(t.amount)} {_describe(t)} -> "
            f"{_category_name(categories_list, t.category_id)}"
            f"{' (human-reviewed)' if t.last_reviewed_at else ' (unreviewed guess)'}"
            for t in state["similar"]
        )
        or NO_SIMILAR
    )
    corrections = (
        "\n".join(
            f"- {t.date} {_money(t.amount)} {_describe(t)}: "
            f"the agent said {_category_name(categories_list, proposed)}, "
            f"a human changed it to {_category_name(categories_list, t.category_id)}"
            for t, proposed in state.get("corrections", [])
        )
        or NO_CORRECTIONS
    )
    entry_side = EntrySide(transaction.entry_side)
    return TRANSACTION_TEMPLATE.format(
        business_name=business.name,
        business_details=business.details or "",
        categories=categories,
        date=transaction.date,
        amount=_money(transaction.amount),
        direction="money out" if entry_side is EntrySide.DEBIT else "money in",
        entry_side=entry_side.value.upper(),
        vendor=transaction.vendor or "(unknown)",
        description=transaction.description or "",
        provider_category=transaction.provider_category or "(none)",
        account_name=account.name if account else "unknown",
        account_type=(account.account_type if account else None) or "unknown",
        account_classification=(
            account.classification.value if account and account.classification else "unknown"
        ),
        similar=similar,
        corrections=corrections,
    )


def _describe(transaction: Transaction) -> str:
    """Merchant plus what the line actually says.

    Showing only the merchant hides the distinction that decides the account:
    "American Express / Platinum Card" and "American Express / Interest
    Payment" are the same merchant and the same direction, but different
    accounts.
    """
    vendor = transaction.vendor or ""
    description = transaction.description or ""
    if description and description.strip().upper() != vendor.strip().upper():
        return f"{vendor} / {description}".strip(" /")
    return vendor or description


def _money(amount: Decimal | None) -> str:
    return f"{amount:,.2f}" if amount is not None else "0.00"


def _category_name(categories: list[Category], category_id: uuid.UUID | None) -> str:
    if category_id is None:
        return "uncategorized"
    return next((c.name for c in categories if c.id == category_id), str(category_id))
