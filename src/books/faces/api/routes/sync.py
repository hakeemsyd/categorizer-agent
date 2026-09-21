from __future__ import annotations

from fastapi import APIRouter

from books.core import repository as repo
from books.core import sync as sync_service
from books.core.errors import ValidationError
from books.core.queue import get_dispatcher
from books.faces.api.deps import AuthDep, SessionDep
from books.faces.api.schemas import SyncRequest, SyncResponse, SyncSummaryOut

router = APIRouter(dependencies=[AuthDep], tags=["sync"])


@router.post("/sync", response_model=SyncResponse)
async def trigger_sync(payload: SyncRequest, session: SessionDep):
    """Queue (or, with ``wait``, run) a sync for one item or a whole business."""
    if payload.item_id:
        items = [await repo.get_item(session, payload.item_id)]
    elif payload.business_id:
        items = list(await repo.list_items(session, business_id=payload.business_id))
    else:
        items = list(await repo.list_items(session, active_only=True))

    if not items:
        raise ValidationError("No items matched the request")

    dispatcher = get_dispatcher()
    results: list[SyncSummaryOut] = []

    for item in items:
        if payload.wait:
            summary = await sync_service.sync_item(
                session, item=item, backfill=payload.backfill, since=payload.since
            )
            # Commit before queueing so the worker cannot read a row that is
            # still inside this request's transaction.
            await session.commit()
            for transaction_id in summary.new_transaction_ids:
                dispatcher.categorize_transaction(transaction_id)
            results.append(
                SyncSummaryOut(
                    item_id=summary.item_id,
                    pages=summary.pages,
                    inserted=summary.inserted,
                    updated=summary.updated,
                    removed=summary.removed,
                    skipped_before_backfill=summary.skipped_before_backfill,
                    new_transaction_ids=summary.new_transaction_ids,
                )
            )
        else:
            task_id = dispatcher.sync_item(item.id, backfill=payload.backfill, since=payload.since)
            results.append(SyncSummaryOut(item_id=item.id, queued_task_id=task_id))

    return SyncResponse(results=results)
