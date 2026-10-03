"""FastAPI core service — the one place every face talks to."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from books import __version__
from books.config import get_settings
from books.core.db import dispose_engine
from books.core.errors import (
    BooksError,
    ConfigurationError,
    NotFoundError,
    ProviderError,
    ValidationError,
    WebhookVerificationError,
)
from books.faces.api.routes import (
    businesses,
    connections,
    health,
    rules,
    sync,
    transactions,
    webhooks,
)
from books.logging import configure_logging, get_logger

log = get_logger(__name__)

# Domain error -> HTTP status, mapped once so core code never imports FastAPI.
_STATUS_BY_ERROR: list[tuple[type[BooksError], int]] = [
    (NotFoundError, status.HTTP_404_NOT_FOUND),
    (ValidationError, 422),  # literal: the constant was renamed across Starlette versions
    (WebhookVerificationError, status.HTTP_401_UNAUTHORIZED),
    (ProviderError, status.HTTP_502_BAD_GATEWAY),
    (ConfigurationError, status.HTTP_500_INTERNAL_SERVER_ERROR),
]


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    log.info("api.startup", version=__version__, env=get_settings().env)
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Books core service",
        version=__version__,
        summary="Bank sync, AI categorization, and human review for multi-business bookkeeping.",
        lifespan=lifespan,
    )

    for router in (
        health.router,
        businesses.router,
        connections.router,
        sync.router,
        transactions.router,
        rules.router,
        webhooks.router,
    ):
        app.include_router(router)

    @app.exception_handler(IntegrityError)
    async def handle_integrity_error(_: Request, exc: IntegrityError) -> JSONResponse:
        # Backstop for constraint violations that slipped past a pre-check:
        # a readable 409 rather than a 500 and a stack trace.
        detail = getattr(getattr(exc, "orig", None), "detail", None) or "Constraint violation"
        log.warning("api.integrity_error", detail=str(detail))
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error": "IntegrityError", "detail": str(detail)},
        )

    @app.exception_handler(BooksError)
    async def handle_books_error(_: Request, exc: BooksError) -> JSONResponse:
        code = next(
            (s for error_type, s in _STATUS_BY_ERROR if isinstance(exc, error_type)),
            status.HTTP_400_BAD_REQUEST,
        )
        if code >= 500:
            log.error("api.error", error=type(exc).__name__, detail=str(exc))
        return JSONResponse(
            status_code=code, content={"error": type(exc).__name__, "detail": str(exc)}
        )

    return app


app = create_app()
