"""Thin, synchronous HTTP client for the Books core service."""

from __future__ import annotations

import uuid
from datetime import date
from types import TracebackType
from typing import Any

import httpx

from books.config import get_settings


class BooksAPIError(RuntimeError):
    def __init__(self, status_code: int, detail: str, error: str | None = None) -> None:
        super().__init__(f"[{status_code}] {error or 'error'}: {detail}")
        self.status_code = status_code
        self.detail = detail
        self.error = error


class BooksClient:
    """Wraps the REST surface. No business logic — just transport."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float | None = None,
    ) -> None:
        settings = get_settings()
        self._client = httpx.Client(
            base_url=(base_url or settings.api_base_url).rstrip("/"),
            headers={"Authorization": f"Bearer {token or settings.api_token}"},
            timeout=timeout or settings.api_timeout_seconds,
        )

    # --- plumbing -------------------------------------------------------

    @property
    def base_url(self) -> str:
        """Which deployment this client is pointed at."""
        return str(self._client.base_url)

    def __enter__(self) -> BooksClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if "params" in kwargs:
            kwargs["params"] = _clean(kwargs["params"])
        if "json" in kwargs and isinstance(kwargs["json"], dict):
            kwargs["json"] = _clean(kwargs["json"])
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.ConnectError as exc:
            raise BooksAPIError(
                503,
                f"Cannot reach the core service at {self._client.base_url}. "
                "Is it running (`make api`)?",
                "ConnectionError",
            ) from exc

        if response.is_success:
            return response.json() if response.content else None

        detail, error = response.text, None
        try:
            body = response.json()
            detail = str(body.get("detail", detail))
            error = body.get("error")
        except ValueError:
            pass
        raise BooksAPIError(response.status_code, detail, error)

    # --- health / tenants / businesses ---------------------------------

    def health(self) -> dict:
        return self._request("GET", "/health")

    def list_tenants(self) -> list[dict]:
        return self._request("GET", "/tenants")

    def create_tenant(self, name: str) -> dict:
        return self._request("POST", "/tenants", json={"name": name})

    def list_businesses(self) -> list[dict]:
        return self._request("GET", "/businesses")

    def create_business(self, name: str, details: str | None = None) -> dict:
        return self._request("POST", "/businesses", json={"name": name, "details": details})

    def get_business(self, business_id: uuid.UUID | str) -> dict:
        return self._request("GET", f"/businesses/{business_id}")

    # --- categories -----------------------------------------------------

    def list_categories(
        self,
        business_id: uuid.UUID | str,
        include_archived: bool = False,
        account_type: str | None = None,
    ) -> list[dict]:
        return self._request(
            "GET",
            f"/businesses/{business_id}/categories",
            params={"include_archived": include_archived, "account_type": account_type},
        )

    def create_category(
        self,
        business_id: uuid.UUID | str,
        name: str,
        account_type: str,
        description: str | None = None,
        parent_category_id: str | None = None,
    ) -> dict:
        return self._request(
            "POST",
            f"/businesses/{business_id}/categories",
            json={
                "name": name,
                "account_type": account_type,
                "description": description,
                "parent_category_id": parent_category_id,
            },
        )

    def seed_chart_of_accounts(self, business_id: uuid.UUID | str) -> dict:
        return self._request("POST", f"/businesses/{business_id}/categories/seed")

    def archive_category(self, category_id: uuid.UUID | str) -> dict:
        return self._request("DELETE", f"/categories/{category_id}")

    # --- linking / items ------------------------------------------------

    def create_link_token(self, business_id: uuid.UUID | str, provider: str | None = None) -> dict:
        return self._request(
            "POST", "/link/token", json={"business_id": str(business_id), "provider": provider}
        )

    def exchange_public_token(
        self,
        business_id: uuid.UUID | str,
        public_token: str,
        provider: str | None = None,
        backfill_start_date: date | None = None,
    ) -> dict:
        return self._request(
            "POST",
            "/link/exchange",
            json={
                "business_id": str(business_id),
                "public_token": public_token,
                "provider": provider,
                "backfill_start_date": backfill_start_date.isoformat()
                if backfill_start_date
                else None,
            },
        )

    def list_items(self, business_id: uuid.UUID | str | None = None) -> list[dict]:
        return self._request("GET", "/items", params={"business_id": business_id})

    def get_item(self, item_id: uuid.UUID | str) -> dict:
        return self._request("GET", f"/items/{item_id}")

    def refresh_accounts(self, item_id: uuid.UUID | str) -> dict:
        return self._request("POST", f"/items/{item_id}/accounts/refresh")

    def force_refresh(self, item_id: uuid.UUID | str) -> dict:
        return self._request("POST", f"/items/{item_id}/refresh")

    def list_accounts(self, business_id: uuid.UUID | str | None = None) -> list[dict]:
        return self._request("GET", "/accounts", params={"business_id": business_id})

    # --- sync -----------------------------------------------------------

    def sync(
        self,
        item_id: uuid.UUID | str | None = None,
        business_id: uuid.UUID | str | None = None,
        backfill: bool = False,
        since: date | None = None,
        wait: bool = False,
    ) -> dict:
        return self._request(
            "POST",
            "/sync",
            json={
                "item_id": str(item_id) if item_id else None,
                "business_id": str(business_id) if business_id else None,
                "backfill": backfill,
                "since": since.isoformat() if since else None,
                "wait": wait,
            },
            timeout=None if wait else self._client.timeout,
        )

    # --- transactions ---------------------------------------------------

    def list_transactions(self, **filters: Any) -> dict:
        return self._request("GET", "/transactions", params=filters)

    def get_transaction(self, transaction_id: uuid.UUID | str) -> dict:
        return self._request("GET", f"/transactions/{transaction_id}")

    def transaction_history(self, transaction_id: uuid.UUID | str) -> list[dict]:
        return self._request("GET", f"/transactions/{transaction_id}/history")

    def categorize(
        self, transaction_id: uuid.UUID | str, wait: bool = False, force: bool = False
    ) -> dict:
        return self._request(
            "POST",
            f"/transactions/{transaction_id}/categorize",
            json={"wait": wait, "force": force},
        )

    def categorize_batch(
        self,
        business_id: uuid.UUID | str,
        account_id: str | None = None,
        category_id: str | None = None,
        category_name: str | None = None,
        needs_review: bool | None = None,
        uncategorized: bool | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        include_reviewed: bool = False,
    ) -> dict:
        return self._request(
            "POST",
            "/transactions/categorize-batch",
            json={
                "business_id": str(business_id),
                "account_id": account_id,
                "category_id": category_id,
                "category_name": category_name,
                "needs_review": needs_review,
                "uncategorized": uncategorized,
                "start_date": start_date,
                "end_date": end_date,
                "include_reviewed": include_reviewed,
            },
        )

    def recategorize(
        self,
        transaction_id: uuid.UUID | str,
        actor: str,
        category_id: str | None = None,
        category_name: str | None = None,
        note: str | None = None,
    ) -> dict:
        return self._request(
            "POST",
            f"/transactions/{transaction_id}/recategorize",
            json={
                "actor": actor,
                "category_id": category_id,
                "category_name": category_name,
                "note": note,
            },
        )

    def confirm(self, transaction_id: uuid.UUID | str, actor: str, note: str | None = None) -> dict:
        return self._request(
            "POST", f"/transactions/{transaction_id}/confirm", json={"actor": actor, "note": note}
        )

    # --- rules ----------------------------------------------------------

    def list_rules(self, business_id: uuid.UUID | str, active_only: bool = True) -> list[dict]:
        return self._request(
            "GET", "/rules", params={"business_id": business_id, "active_only": active_only}
        )

    def create_rule(
        self,
        business_id: uuid.UUID | str,
        match_type: str,
        pattern: str,
        category_name: str | None = None,
        category_id: str | None = None,
        note: str | None = None,
        priority: int = 100,
        created_by: str | None = None,
    ) -> dict:
        return self._request(
            "POST",
            "/rules",
            json={
                "business_id": str(business_id),
                "match_type": match_type,
                "pattern": pattern,
                "category_name": category_name,
                "category_id": category_id,
                "note": note,
                "priority": priority,
                "created_by": created_by,
            },
        )

    def deactivate_rule(self, rule_id: uuid.UUID | str) -> dict:
        return self._request("DELETE", f"/rules/{rule_id}")


def _clean(payload: dict) -> dict:
    """Drop ``None`` values so optional filters don't become literal 'None'."""
    return {
        k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in payload.items() if v is not None
    }
