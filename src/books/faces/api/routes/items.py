from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from books.config import get_settings
from books.core import repository as repo
from books.core import sync as sync_service
from books.faces.api.deps import AuthDep, SessionDep
from books.faces.api.schemas import (
    AccountOut,
    Acknowledgement,
    ItemOut,
    LinkExchange,
    LinkTokenOut,
    LinkTokenRequest,
)

router = APIRouter(dependencies=[AuthDep], tags=["items"])


@router.post("/link/token", response_model=LinkTokenOut)
async def create_link_token(payload: LinkTokenRequest, session: SessionDep):
    business = await repo.get_business(session, payload.business_id)
    provider_name = payload.provider or get_settings().default_provider
    token = sync_service.create_link_token(
        provider_name=provider_name, user_ref=str(business.tenant_id)
    )
    return LinkTokenOut(link_token=token.token, expiration=token.expiration, provider=provider_name)


@router.post("/link/exchange", response_model=ItemOut, status_code=status.HTTP_201_CREATED)
async def exchange_public_token(payload: LinkExchange, session: SessionDep):
    business = await repo.get_business(session, payload.business_id)
    return await sync_service.link_item(
        session,
        business=business,
        provider_name=payload.provider or get_settings().default_provider,
        public_token=payload.public_token,
        backfill_start_date=payload.backfill_start_date,
    )


@router.get("/items", response_model=list[ItemOut])
async def list_items(session: SessionDep, business_id: uuid.UUID | None = None):
    return await repo.list_items(session, business_id=business_id)


@router.get("/items/{item_id}", response_model=ItemOut)
async def get_item(item_id: uuid.UUID, session: SessionDep):
    return await repo.get_item(session, item_id)


@router.post("/items/{item_id}/accounts/refresh", response_model=Acknowledgement)
async def refresh_accounts(item_id: uuid.UUID, session: SessionDep):
    item = await repo.get_item(session, item_id)
    count = await sync_service.refresh_accounts(session, item=item)
    return Acknowledgement(detail=f"Refreshed {count} account(s)")


@router.post("/items/{item_id}/refresh", response_model=Acknowledgement)
async def force_refresh(item_id: uuid.UUID, session: SessionDep):
    """Ask the provider to pull fresh data now; the webhook follows."""
    item = await repo.get_item(session, item_id)
    sync_service.force_refresh(item)
    return Acknowledgement(detail="Provider refresh requested")


@router.get("/accounts", response_model=list[AccountOut])
async def list_accounts(session: SessionDep, business_id: uuid.UUID | None = None):
    return await repo.list_accounts(session, business_id=business_id)
