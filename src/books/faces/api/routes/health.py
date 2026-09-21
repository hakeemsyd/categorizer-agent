from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from books import __version__
from books.config import get_settings
from books.faces.api.deps import SessionDep
from books.faces.api.schemas import HealthOut
from books.providers.registry import available_providers

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthOut)
async def health(session: SessionDep) -> HealthOut:
    try:
        await session.execute(text("SELECT 1"))
        database = "ok"
    except Exception as exc:  # surfaced rather than raised: health must answer
        database = f"error: {type(exc).__name__}"
    return HealthOut(
        status="ok" if database == "ok" else "degraded",
        version=__version__,
        env=get_settings().env,
        database=database,
        providers=available_providers(),
    )
