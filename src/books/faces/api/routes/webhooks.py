"""Provider webhook receiver.

Acknowledges fast and hands the real work to Celery (plan.md §5, step 3). The
path carries the provider name so a second aggregator needs no new route logic.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request

from books.core import repository as repo
from books.core.errors import WebhookVerificationError
from books.core.queue import get_dispatcher
from books.faces.api.deps import SessionDep
from books.faces.api.schemas import Acknowledgement
from books.logging import get_logger
from books.providers import get_provider
from books.providers.base import EVENT_ITEM_ERROR, EVENT_ITEM_REVOKED, EVENT_SYNC_AVAILABLE

router = APIRouter(tags=["webhooks"])
log = get_logger(__name__)


@router.post("/webhooks/{provider_name}", response_model=Acknowledgement)
async def receive_webhook(provider_name: str, request: Request, session: SessionDep):
    # Webhooks authenticate by provider signature, not by our bearer token,
    # so this route deliberately sits outside the AuthDep dependency.
    body = await request.body()
    provider = get_provider(provider_name)
    provider.verify_webhook(dict(request.headers), body)

    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError as exc:
        raise WebhookVerificationError("Webhook body is not valid JSON") from exc

    event = provider.parse_webhook(payload)
    connection = await repo.get_connection_by_provider_ref(
        session, provider=provider_name, provider_ref=event.provider_ref
    )
    if connection is None:
        log.warning(
            "webhook.unknown_connection",
            provider=provider_name,
            provider_ref=event.provider_ref,
        )
        return Acknowledgement(ok=True, detail="Unknown connection; ignored")

    log.info(
        "webhook.received",
        provider=provider_name,
        connection_id=str(connection.id),
        # not `event=`: structlog reserves that key for the log message itself
        event_type=event.event_type,
    )

    if event.event_type == EVENT_SYNC_AVAILABLE:
        get_dispatcher().sync_connection(connection.id, backfill=False, since=None)
        return Acknowledgement(detail="Sync queued")

    if event.event_type in (EVENT_ITEM_ERROR, EVENT_ITEM_REVOKED):
        connection.status = "error" if event.event_type == EVENT_ITEM_ERROR else "disconnected"
        connection.last_error = json.dumps(payload.get("error") or event.event_type)[:2000]
        return Acknowledgement(detail=f"Connection marked {connection.status}")

    return Acknowledgement(detail="Event ignored")
