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
    ConnectionOut,
    LinkExchange,
    LinkTokenOut,
    LinkTokenRequest,
)

router = APIRouter(dependencies=[AuthDep], tags=["connections"])


@router.post("/link/token", response_model=LinkTokenOut)
async def create_link_token(payload: LinkTokenRequest, session: SessionDep):
    business = await repo.get_business(session, payload.business_id)
    provider_name = payload.provider or get_settings().default_provider
    token = sync_service.create_link_token(
        provider_name=provider_name, user_ref=str(business.tenant_id)
    )
    # Fintable's OAuth flow needs more than a token: the browser has to be
    # sent to a real authorization URL with a PKCE challenge the face mints,
    # so the face is told where to go rather than guessing the host.
    settings = get_settings()
    return LinkTokenOut(
        link_token=token.token,
        expiration=token.expiration,
        provider=provider_name,
        environment="",
        authorize_url=f"{settings.fintable_base_url.rstrip('/')}/oauth/authorize",
        redirect_uri=settings.fintable_redirect_uri,
        scopes=settings.fintable_scopes,
    )


@router.post("/link/exchange", response_model=ConnectionOut, status_code=status.HTTP_201_CREATED)
async def exchange_public_token(payload: LinkExchange, session: SessionDep):
    business = await repo.get_business(session, payload.business_id)
    return await sync_service.link_connection(
        session,
        business=business,
        provider_name=payload.provider or get_settings().default_provider,
        public_token=payload.public_token,
        backfill_start_date=payload.backfill_start_date,
    )


@router.get("/connections", response_model=list[ConnectionOut])
async def list_connections(session: SessionDep, business_id: uuid.UUID | None = None):
    return await repo.list_connections(session, business_id=business_id)


@router.get("/connections/{connection_id}", response_model=ConnectionOut)
async def get_connection(connection_id: uuid.UUID, session: SessionDep):
    return await repo.get_connection(session, connection_id)


@router.post("/connections/{connection_id}/accounts/refresh", response_model=Acknowledgement)
async def refresh_accounts(connection_id: uuid.UUID, session: SessionDep):
    connection = await repo.get_connection(session, connection_id)
    count = await sync_service.refresh_accounts(session, connection=connection)
    return Acknowledgement(detail=f"Refreshed {count} account(s)")


@router.post("/connections/{connection_id}/refresh", response_model=Acknowledgement)
async def force_refresh(connection_id: uuid.UUID, session: SessionDep):
    """Ask Fintable to pull from the banks now.

    There is no webhook to follow it — Fintable documents none — so the new
    data arrives on the next scheduled or manual sync.
    """
    connection = await repo.get_connection(session, connection_id)
    await sync_service.force_refresh(session, connection=connection)
    return Acknowledgement(detail="Provider refresh requested")


@router.get("/accounts", response_model=list[AccountOut])
async def list_accounts(session: SessionDep, business_id: uuid.UUID | None = None):
    return await repo.list_accounts(session, business_id=business_id)
