from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Query

from books.agent.categorizer import categorize_transaction as run_categorizer
from books.core import repository as repo
from books.core.categorization import apply_category, confirm_category
from books.core.errors import ValidationError
from books.core.queue import get_dispatcher
from books.faces.api.deps import AuthDep, SessionDep
from books.faces.api.schemas import (
    CategorizeRequest,
    CategorizeResponse,
    ConfirmRequest,
    HistoryOut,
    RecategorizeRequest,
    TransactionOut,
    TransactionPage,
)

router = APIRouter(dependencies=[AuthDep], tags=["transactions"])


@router.get("/transactions", response_model=TransactionPage)
async def list_transactions(
    session: SessionDep,
    business_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    category_id: uuid.UUID | None = None,
    needs_review: bool | None = None,
    uncategorized: bool | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    search: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    filters = repo.TransactionFilters(
        business_id=business_id,
        account_id=account_id,
        category_id=category_id,
        needs_review=needs_review,
        uncategorized=uncategorized,
        start_date=start_date,
        end_date=end_date,
        search=search,
        limit=limit,
        offset=offset,
    )
    return TransactionPage(
        total=await repo.count_transactions(session, filters),
        limit=limit,
        offset=offset,
        items=[
            TransactionOut.model_validate(t) for t in await repo.list_transactions(session, filters)
        ],
    )


@router.get("/transactions/{transaction_id}", response_model=TransactionOut)
async def get_transaction(transaction_id: uuid.UUID, session: SessionDep):
    return await repo.get_transaction(session, transaction_id)


@router.get("/transactions/{transaction_id}/history", response_model=list[HistoryOut])
async def transaction_history(transaction_id: uuid.UUID, session: SessionDep):
    await repo.get_transaction(session, transaction_id)
    return await repo.list_history(session, transaction_id=transaction_id)


@router.post("/transactions/{transaction_id}/categorize", response_model=CategorizeResponse)
async def categorize(transaction_id: uuid.UUID, payload: CategorizeRequest, session: SessionDep):
    """Ask the agent to categorize. Queued by default; ``wait`` runs inline."""
    transaction = await repo.get_transaction(session, transaction_id)
    if not payload.wait:
        task_id = get_dispatcher().categorize_transaction(transaction.id)
        return CategorizeResponse(transaction_id=transaction.id, queued_task_id=task_id)

    outcome = await run_categorizer(session, transaction_id=transaction.id)
    return CategorizeResponse(
        transaction_id=outcome.transaction_id,
        category_id=outcome.category_id,
        category_name=outcome.category_name,
        account_type=outcome.account_type,
        confidence=outcome.confidence,
        needs_review=outcome.needs_review,
        rationale=outcome.rationale,
        source=outcome.source,
    )


@router.post("/transactions/{transaction_id}/recategorize", response_model=TransactionOut)
async def recategorize(
    transaction_id: uuid.UUID, payload: RecategorizeRequest, session: SessionDep
):
    """A human sets the category. This is the only path that marks it reviewed."""
    transaction = await repo.get_transaction(session, transaction_id)
    category_id = payload.category_id

    if category_id is None and payload.category_name:
        category = await repo.find_category_by_name(
            session, business_id=transaction.business_id, name=payload.category_name
        )
        if category is None:
            raise ValidationError(
                f"No category named {payload.category_name!r} in this business's chart of accounts"
            )
        category_id = category.id

    if category_id is None:
        raise ValidationError("Provide category_id or category_name")

    return await apply_category(
        session,
        transaction=transaction,
        category_id=category_id,
        actor=payload.actor,
        confidence=1.0,
        rationale=payload.note or "Set by human review",
        mark_reviewed=True,
    )


@router.post("/transactions/{transaction_id}/confirm", response_model=TransactionOut)
async def confirm(transaction_id: uuid.UUID, payload: ConfirmRequest, session: SessionDep):
    transaction = await repo.get_transaction(session, transaction_id)
    return await confirm_category(
        session, transaction=transaction, actor=payload.actor, note=payload.note
    )
