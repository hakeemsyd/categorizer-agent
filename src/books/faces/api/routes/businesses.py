from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from books.agent.chart_builder import ProposedCategory, apply_proposal, build_chart
from books.core import repository as repo
from books.core.accounting import AccountType
from books.core.errors import ValidationError
from books.faces.api.deps import AuthDep, SessionDep
from books.faces.api.schemas import (
    BuildChartRequest,
    BuildChartResponse,
    BusinessCreate,
    BusinessOut,
    CategoryCreate,
    CategoryOut,
    ProposedCategoryOut,
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


@router.post(
    "/businesses/{business_id}/categories/bootstrap",
    response_model=BuildChartResponse,
    tags=["categories"],
)
async def bootstrap_categories(
    business_id: uuid.UUID, payload: BuildChartRequest, session: SessionDep
):
    """Propose a chart of accounts from the merchants already synced.

    Step one of a cold start: the categorizer can only pick from accounts that
    exist, so the buckets have to be right before anything is sorted into them.
    Defaults to proposing only. To create them, call again with ``apply`` and
    the ``proposed`` list you were given: approval should create what the human
    actually read, not whatever a second model call happens to say.
    """
    if payload.proposed is not None:
        if not payload.apply:
            raise ValidationError("Sending `proposed` only makes sense with `apply` set.")
        business = await repo.get_business(session, business_id)
        result = await apply_proposal(
            session,
            business=business,
            proposed=[ProposedCategory(**p.model_dump()) for p in payload.proposed],
        )
    else:
        result = await build_chart(
            session,
            business_id=business_id,
            apply=payload.apply,
            max_merchants=payload.max_merchants,
        )
    return BuildChartResponse(
        merchants_seen=result.merchants_seen,
        proposed=[ProposedCategoryOut(**p.model_dump()) for p in result.proposed],
        created=[CategoryOut.model_validate(c) for c in result.created],
        skipped=result.skipped_existing,
        applied=payload.apply,
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
