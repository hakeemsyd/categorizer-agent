from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Query

from books.agent.bootstrap import bootstrap_business
from books.agent.categorizer import categorize_transaction as run_categorizer
from books.core import repository as repo
from books.core.categorization import apply_category, confirm_category, propagate_correction
from books.core.errors import ValidationError
from books.core.queue import get_dispatcher
from books.faces.api.deps import AuthDep, SessionDep
from books.faces.api.schemas import (
    BootstrapRequest,
    BootstrapResponse,
    CategorizeBatchRequest,
    CategorizeBatchResponse,
    CategorizeRequest,
    CategorizeResponse,
    ConfirmRequest,
    HistoryOut,
    RecategorizeRequest,
    RecategorizeResponse,
    TransactionOut,
    TransactionPage,
    VendorDecisionOut,
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
    reviewed: bool | None = None,
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
        reviewed=reviewed,
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
    """Ask the agent to categorize. Queued by default; ``wait`` runs inline.

    Refuses (422) if a human already reviewed this transaction, unless
    ``force`` is set.
    """
    transaction = await repo.get_transaction(session, transaction_id)
    if not payload.wait:
        task_id = get_dispatcher().categorize_transaction(transaction.id, force=payload.force)
        return CategorizeResponse(transaction_id=transaction.id, queued_task_id=task_id)

    outcome = await run_categorizer(session, transaction_id=transaction.id, force=payload.force)
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


@router.post("/transactions/categorize-batch", response_model=CategorizeBatchResponse)
async def categorize_batch(payload: CategorizeBatchRequest, session: SessionDep):
    """Queue the agent to run again over transactions matching the filters.

    Always async — this can match thousands of rows, and running the model
    that many times inline in one request isn't reasonable. The query itself
    runs here (fast, indexed), so the response reports an accurate count
    immediately; the individual categorizations happen in the background.
    """
    business = await repo.get_business(session, payload.business_id)
    category_id = payload.category_id
    if category_id is None and payload.category_name:
        category = await repo.find_category_by_name(
            session, business_id=business.id, name=payload.category_name
        )
        if category is None:
            raise ValidationError(
                f"No category named {payload.category_name!r} in this business's chart of accounts"
            )
        category_id = category.id

    filters = repo.TransactionFilters(
        business_id=payload.business_id,
        account_id=payload.account_id,
        category_id=category_id,
        needs_review=payload.needs_review,
        uncategorized=payload.uncategorized,
        start_date=payload.start_date,
        end_date=payload.end_date,
        # Skip already-reviewed rows by default; apply_category's own guard
        # is the durable backstop, but there's no reason to even queue a task
        # that's just going to be refused.
        reviewed=None if payload.include_reviewed else False,
        limit=10_000,
    )
    transactions = await repo.list_transactions(session, filters)

    dispatcher = get_dispatcher()
    for transaction in transactions:
        dispatcher.categorize_transaction(transaction.id, force=payload.include_reviewed)

    return CategorizeBatchResponse(queued=len(transactions))


@router.post("/transactions/bootstrap", response_model=BootstrapResponse)
async def bootstrap(payload: BootstrapRequest, session: SessionDep):
    """First-pass categorization for a business with no history.

    Runs inline rather than queued: it is a one-off setup step a human is
    waiting on, and it is one model call per *merchant* — far fewer than per
    transaction. Use ``max_vendors`` for a trial run before committing to the
    whole import.
    """
    result = await bootstrap_business(
        session, business_id=payload.business_id, max_vendors=payload.max_vendors
    )
    return BootstrapResponse(
        vendors_seen=result.vendors_seen,
        vendors_decided=result.vendors_decided,
        transactions_categorized=result.transactions_categorized,
        decisions=[
            VendorDecisionOut(
                vendor_label=d.vendor_label,
                category_name=d.category_name,
                confidence=d.confidence,
                rationale=d.rationale,
                applied=d.applied,
                error=d.error,
            )
            for d in result.decisions
        ],
    )


@router.post("/transactions/{transaction_id}/recategorize", response_model=RecategorizeResponse)
async def recategorize(
    transaction_id: uuid.UUID, payload: RecategorizeRequest, session: SessionDep
):
    """A human sets the category. This is the only path that marks it reviewed.

    By default the decision also reaches the same merchant's other unreviewed
    transactions, so correcting one row fixes the ones like it.
    """
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

    await apply_category(
        session,
        transaction=transaction,
        category_id=category_id,
        actor=payload.actor,
        confidence=1.0,
        rationale=payload.note or "Set by human review",
        mark_reviewed=True,
    )

    propagation = None
    if payload.propagate:
        propagation = await propagate_correction(
            session, transaction=transaction, actor=payload.actor
        )

    return RecategorizeResponse(
        transaction=TransactionOut.model_validate(transaction),
        also_updated=propagation.count if propagation else 0,
        skipped_reviewed=propagation.skipped_reviewed if propagation else 0,
        vendor_label=propagation.vendor_label if propagation else None,
    )


@router.post("/transactions/{transaction_id}/confirm", response_model=TransactionOut)
async def confirm(transaction_id: uuid.UUID, payload: ConfirmRequest, session: SessionDep):
    transaction = await repo.get_transaction(session, transaction_id)
    return await confirm_category(
        session, transaction=transaction, actor=payload.actor, note=payload.note
    )
