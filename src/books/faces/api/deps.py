"""FastAPI dependencies: auth and a request-scoped session."""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from books.config import get_settings
from books.core.db import get_sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
    """Shared bearer token between the core and its faces.

    v1 is single-tenant, so this is a deployment secret rather than a user
    identity. Per-tenant auth arrives with the SaaS phase (plan.md §8).
    """
    expected = get_settings().api_token
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


SessionDep = Annotated[AsyncSession, Depends(get_session)]
AuthDep = Depends(require_token)
