from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from books.core import repository as repo
from books.core.accounting import AccountType
from books.faces.api.deps import AuthDep, SessionDep
from books.faces.api.schemas import (
    BusinessCreate,
    BusinessOut,
    CategoryCreate,
    CategoryOut,
    SeedChartResponse,
    TenantCreate,
    TenantOut,
)

router = APIRouter(dependencies=[AuthDep])


@router.get("/tenants", response_model=list[TenantOut], tags=["tenants"])
async def list_tenants(session: SessionDep):
    return await repo.list_tenants(session)


@router.post(
    "/tenants", response_model=TenantOut, status_code=status.HTTP_201_CREATED, tags=["tenants"]
)
async def create_tenant(payload: TenantCreate, session: SessionDep):
    return await repo.create_tenant(session, name=payload.name)


@router.get("/businesses", response_model=list[BusinessOut], tags=["businesses"])
async def list_businesses(session: SessionDep, tenant_id: uuid.UUID | None = None):
    return await repo.list_businesses(session, tenant_id=tenant_id)


@router.post(
    "/businesses",
    response_model=BusinessOut,
    status_code=status.HTTP_201_CREATED,
    tags=["businesses"],
)
async def create_business(payload: BusinessCreate, session: SessionDep):
    tenant_id = payload.tenant_id or (await repo.default_tenant(session)).id
    return await repo.create_business(
        session, tenant_id=tenant_id, name=payload.name, details=payload.details
    )


@router.get("/businesses/{business_id}", response_model=BusinessOut, tags=["businesses"])
async def get_business(business_id: uuid.UUID, session: SessionDep):
    return await repo.get_business(session, business_id)


@router.get(
    "/businesses/{business_id}/categories", response_model=list[CategoryOut], tags=["categories"]
)
async def list_categories(
    business_id: uuid.UUID,
    session: SessionDep,
    include_archived: bool = False,
    account_type: AccountType | None = None,
):
    return await repo.list_categories(
        session,
        business_id=business_id,
        include_archived=include_archived,
        account_type=account_type,
    )


@router.post(
    "/businesses/{business_id}/categories",
    response_model=CategoryOut,
    status_code=status.HTTP_201_CREATED,
    tags=["categories"],
)
async def create_category(business_id: uuid.UUID, payload: CategoryCreate, session: SessionDep):
    business = await repo.get_business(session, business_id)
    return await repo.create_category(
        session,
        business=business,
        name=payload.name,
        account_type=payload.account_type,
        description=payload.description,
        parent_category_id=payload.parent_category_id,
    )


@router.post(
    "/businesses/{business_id}/categories/seed",
    response_model=SeedChartResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["categories"],
)
async def seed_categories(business_id: uuid.UUID, session: SessionDep):
    """Create the default chart of accounts. Idempotent — existing names are kept."""
    business = await repo.get_business(session, business_id)
    created, skipped = await repo.seed_chart_of_accounts(session, business=business)
    return SeedChartResponse(
        created=[CategoryOut.model_validate(c) for c in created], skipped=skipped
    )


@router.delete(
    "/categories/{category_id}",
    response_model=CategoryOut,
    tags=["categories"],
)
async def archive_category(category_id: uuid.UUID, session: SessionDep):
    """Archive rather than delete — history rows still reference it."""
    category = await repo.get_category(session, category_id)
    category.archived = True
    await session.flush()
    return category
