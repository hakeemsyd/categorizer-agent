from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from books.core import repository as repo
from books.core.errors import ValidationError
from books.faces.api.deps import AuthDep, SessionDep
from books.faces.api.schemas import RuleCreate, RuleOut

router = APIRouter(dependencies=[AuthDep], tags=["rules"])


@router.get("/rules", response_model=list[RuleOut])
async def list_rules(session: SessionDep, business_id: uuid.UUID, active_only: bool = True):
    return await repo.list_rules(session, business_id=business_id, active_only=active_only)


@router.post("/rules", response_model=RuleOut, status_code=status.HTTP_201_CREATED)
async def create_rule(payload: RuleCreate, session: SessionDep):
    business = await repo.get_business(session, payload.business_id)
    category_id = payload.category_id

    if category_id is None and payload.category_name:
        category = await repo.find_category_by_name(
            session, business_id=business.id, name=payload.category_name
        )
        if category is None:
            raise ValidationError(f"No category named {payload.category_name!r}")
        category_id = category.id
    if category_id is None:
        raise ValidationError("Provide category_id or category_name")

    return await repo.create_rule(
        session,
        business=business,
        match_type=payload.match_type,
        pattern=payload.pattern,
        category_id=category_id,
        note=payload.note,
        priority=payload.priority,
        created_by=payload.created_by,
    )


@router.delete("/rules/{rule_id}", response_model=RuleOut)
async def deactivate_rule(rule_id: uuid.UUID, session: SessionDep):
    return await repo.deactivate_rule(session, rule_id)
