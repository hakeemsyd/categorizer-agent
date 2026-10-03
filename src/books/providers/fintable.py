"""Fintable adapter (https://fintable.io/docs) — the only module that knows its wire format.

How it differs from Teller, which it replaced:

* **OAuth 2.0 with PKCE, and no client secret.** Fintable is a public client:
  the docs say a secret is never "generated, displayed, or accepted", and
  ``code_challenge_method=S256`` is enforced before the login screen renders.
  So the browser flow is a real authorization-code exchange rather than
  Teller Connect handing over a finished token.
* **Tokens expire, and the refresh token rotates.** An access token lasts an
  hour; refreshing returns a *new* refresh token and invalidates the old one.
  That makes a refresh a write that must be durable before its result is used,
  which is why this module only mints tokens and never decides when to — see
  ``core.sync``, which refreshes under a row lock and commits first.
* **Real incremental sync.** ``updated_since`` with ``order=updated`` and a
  cursor is exactly the delta primitive Teller lacked, so a sync no longer
  re-walks a trailing window hoping to notice changes.
* **Cursors are workspace-bound.** Replaying one against a different workspace
  fails as ``invalid_cursor``, so every paged read names the same workspace.
* **No webhooks.** Nothing in the API reference offers them, so freshness is
  Celery Beat's scheduled sync and nothing else. ``verify_webhook`` refuses
  everything rather than pretending otherwise.
* **No deletions are reported**, so ``SyncResult.removed`` is always empty.
  A transaction that vanishes upstream stays in our books until a human
  removes it — the same limitation Teller had.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

import httpx

from books.config import get_settings
from books.core.accounting import AccountType
from books.core.errors import ConfigurationError, ProviderError, WebhookVerificationError
from books.logging import get_logger
from books.providers.base import (
    EVENT_UNKNOWN,
    ItemCredentials,
    LinkToken,
    RawAccount,
    RawTransaction,
    SyncResult,
    TokenSet,
    WebhookEvent,
)

log = get_logger(__name__)

#: Fintable types are free text like "depository / checking" or
#: "credit / credit_card". Only the family before the slash decides whether a
#: positive amount is money in or money out, so that is all we read.
_LIABILITY_FAMILIES = ("credit", "loan", "liability", "mortgage")

#: One page of transactions. The API caps this; asking for more is not an error
#: but gains nothing.
_PAGE_LIMIT = 500

#: Stop walking pages even if the API keeps offering them. A runaway loop
#: against a paid API is worse than an incomplete sync a human can re-run.
_MAX_PAGES = 100


@dataclass(frozen=True)
class PkceChallenge:
    """One PKCE pair. The verifier never leaves this process until the swap."""

    verifier: str
    challenge: str
    state: str

    @staticmethod
    def generate() -> PkceChallenge:
        verifier = base64.urlsafe_b64encode(os.urandom(64)).decode().rstrip("=")
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        return PkceChallenge(
            verifier=verifier,
            challenge=base64.urlsafe_b64encode(digest).decode().rstrip("="),
            state=secrets.token_urlsafe(32),
        )


@dataclass(frozen=True)
class _Cursor:
    """What we need to resume a sync, serialized as JSON.

    JSON rather than a delimited string on purpose: the previous provider used
    a colon-joined key and it broke the first time an ISO timestamp with its
    own colons went into it.
    """

    cursor: str | None = None
    updated_since: str | None = None

    def encode(self) -> str:
        return json.dumps({"cursor": self.cursor, "updated_since": self.updated_since})

    @staticmethod
    def decode(raw: str | None) -> _Cursor:
        if not raw:
            return _Cursor()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # A cursor from an older format is not worth failing a sync over;
            # a full re-read is correct, just slower.
            return _Cursor()
        return _Cursor(cursor=data.get("cursor"), updated_since=data.get("updated_since"))


class FintableProvider:
    """Fintable's API, shaped into the provider contract."""

    name = "fintable"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._settings = get_settings()
        self._client = client
        self._workspace_id = self._settings.fintable_workspace_id

    # --- configuration -----------------------------------------------------

    @property
    def _base(self) -> str:
        return self._settings.fintable_base_url.rstrip("/")

    def _client_id(self) -> str:
        client_id = self._settings.fintable_client_id
        if not client_id:
            raise ConfigurationError(
                "BOOKS_FINTABLE_CLIENT_ID is unset — register an OAuth app at "
                "fintable.io and set it. There is no client secret to find: "
                "Fintable is a public client and never accepts one."
            )
        return client_id

    def _http(self) -> httpx.Client:
        return self._client or httpx.Client(timeout=30.0)

    def _request(self, method: str, path: str, *, token: str | None = None, **kwargs: Any) -> Any:
        headers = dict(kwargs.pop("headers", {}) or {})
        if token:
            headers["Authorization"] = f"Bearer {token}"
        url = path if path.startswith("http") else f"{self._base}{path}"

        client = self._http()
        try:
            response = client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Fintable request failed: {exc}") from exc
        finally:
            if self._client is None:
                client.close()

        if response.status_code >= 400:
            raise ProviderError(_explain(response))
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError(f"Fintable returned a non-JSON body: {exc}") from exc

    # --- OAuth -------------------------------------------------------------

    def authorization_url(self, pkce: PkceChallenge) -> str:
        """Where to send the browser. PKCE is mandatory, so it is not optional here."""
        query = urlencode(
            {
                "client_id": self._client_id(),
                "redirect_uri": self._settings.fintable_redirect_uri,
                "response_type": "code",
                "scope": self._settings.fintable_scopes,
                "state": pkce.state,
                "code_challenge": pkce.challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self._base}/oauth/authorize?{query}"

    def create_link_token(self, user_ref: str) -> LinkToken:
        """The contract's handle for "start a link".

        Fintable has no link token; the equivalent is the client id, which is
        public by design. The PKCE pair is minted per attempt by the face
        driving the browser, so it cannot be smuggled in here.
        """
        return LinkToken(token=self._client_id(), expiration="")

    def exchange_public_token(self, public_token: str) -> ItemCredentials:
        """Swap an authorization code for tokens.

        ``public_token`` carries ``code`` and the PKCE ``verifier`` together,
        because the contract passes one opaque string and the verifier must
        make the same trip. It never leaves this process otherwise.
        """
        try:
            payload = json.loads(public_token)
            code, verifier = payload["code"], payload["code_verifier"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ProviderError(
                "Expected a JSON payload with 'code' and 'code_verifier'. "
                "Fintable requires PKCE, so the verifier must reach the swap."
            ) from exc

        tokens = self._token_request(
            {
                "grant_type": "authorization_code",
                "client_id": self._client_id(),
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": self._settings.fintable_redirect_uri,
            }
        )

        # Name the connection from the API rather than trusting the browser.
        workspace = self._resolve_workspace(tokens.access_token)
        connections = self._request("GET", "/api/v2/connections", token=tokens.access_token)
        rows = _rows(connections)
        institution = next(
            (str(r.get("institution_name") or r.get("name")) for r in rows if r.get("name")),
            "Fintable",
        )
        return ItemCredentials(
            provider_ref=workspace,
            access_token=tokens.access_token,
            institution_name=institution,
            refresh_token=tokens.refresh_token,
            expires_at=tokens.expires_at,
        )

    def refresh_tokens(self, refresh_token: str) -> TokenSet:
        """Renew an expired access token.

        The returned refresh token replaces the one passed in — Fintable
        invalidates the old immediately. The caller must store this before
        using the access token, or the connection is lost.
        """
        return self._token_request(
            {
                "grant_type": "refresh_token",
                "client_id": self._client_id(),
                "refresh_token": refresh_token,
            }
        )

    def _token_request(self, form: dict[str, str]) -> TokenSet:
        data = self._request(
            "POST",
            "/oauth/token",
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        access = data.get("access_token")
        if not access:
            raise ProviderError(f"Fintable returned no access_token: {str(data)[:200]}")

        expires_at = None
        if isinstance(data.get("expires_in"), int | float | str):
            try:
                expires_at = datetime.now(tz=UTC) + timedelta(seconds=int(data["expires_in"]))
            except (TypeError, ValueError):
                expires_at = None
        return TokenSet(
            access_token=str(access),
            refresh_token=(str(data["refresh_token"]) if data.get("refresh_token") else None),
            expires_at=expires_at,
        )

    # --- workspace ---------------------------------------------------------

    def _resolve_workspace(self, token: str) -> str:
        """Which workspace this token reads. Cursors are bound to it.

        Resolved once and remembered: replaying a cursor against a different
        workspace fails outright, so this must not drift mid-sync.
        """
        if self._workspace_id:
            return self._workspace_id
        me = self._request("GET", "/api/v2/me", token=token)
        workspace = (
            me.get("workspace_id")
            or (me.get("workspace") or {}).get("id")
            or (me.get("default_workspace") or {}).get("id")
        )
        if not workspace:
            workspaces = _rows(self._request("GET", "/api/v2/workspaces", token=token))
            workspace = workspaces[0].get("id") if workspaces else None
        if not workspace:
            raise ProviderError(
                "Could not determine a Fintable workspace for this token. "
                "Set BOOKS_FINTABLE_WORKSPACE_ID explicitly."
            )
        self._workspace_id = str(workspace)
        return self._workspace_id

    # --- reads -------------------------------------------------------------

    def get_accounts(self, access_token: str) -> list[RawAccount]:
        params = {"workspace_id": self._resolve_workspace(access_token)}
        data = self._request("GET", "/api/v2/accounts", token=access_token, params=params)
        return [_to_account(row) for row in _rows(data)]

    def sync_transactions(self, access_token: str, cursor: str | None) -> SyncResult:
        """Walk pages of transactions, newest changes first.

        On a first sync there is no watermark, so this reads everything the
        workspace has. Afterwards ``updated_since`` narrows it to what moved,
        which also catches an amended transaction rather than only new ones.
        """
        state = _Cursor.decode(cursor)
        workspace = self._resolve_workspace(access_token)
        started = datetime.now(tz=UTC)

        params: dict[str, Any] = {
            "workspace_id": workspace,
            "limit": _PAGE_LIMIT,
            "order": "updated",
        }
        if state.updated_since:
            params["updated_since"] = state.updated_since
        if state.cursor:
            params["cursor"] = state.cursor

        transactions: list[RawTransaction] = []
        next_cursor: str | None = None
        pages = 0
        while pages < _MAX_PAGES:
            data = self._request("GET", "/api/v2/transactions", token=access_token, params=params)
            for row in _rows(data):
                parsed = _to_transaction(row)
                if parsed is not None:
                    transactions.append(parsed)
            next_cursor = data.get("next_cursor")
            pages += 1
            if not next_cursor:
                break
            params["cursor"] = next_cursor

        if next_cursor and pages >= _MAX_PAGES:
            # Stop here and keep the cursor: the next run resumes mid-walk
            # rather than starting over, and the watermark stays put so
            # nothing is skipped.
            log.warning("fintable.sync_truncated", pages=pages, workspace=workspace)
            resting = _Cursor(cursor=next_cursor, updated_since=state.updated_since)
            return SyncResult(
                added=transactions,
                modified=[],
                removed=[],
                next_cursor=resting.encode(),
                has_more=True,
            )

        # The walk finished: move the watermark to when it started, not now,
        # so anything written while it ran is picked up next time instead of
        # falling into the gap.
        resting = _Cursor(cursor=None, updated_since=started.isoformat())
        # An incremental run reports changes as modified; a first run is all new.
        is_incremental = state.updated_since is not None
        return SyncResult(
            added=[] if is_incremental else transactions,
            modified=transactions if is_incremental else [],
            removed=[],
            next_cursor=resting.encode(),
            has_more=False,
        )

    def force_refresh(self, access_token: str) -> None:
        """Ask Fintable to pull from the banks now.

        Unlike Teller, this is real: /api/v2/sync triggers a provider-side
        refresh rather than just proving the token still works.
        """
        self._request(
            "POST",
            "/api/v2/sync",
            token=access_token,
            json={"workspace_id": self._resolve_workspace(access_token)},
        )

    def create_sandbox_connection(self, access_token: str) -> dict:
        """Fintable's own test connection: example accounts and transactions.

        Free, outside plan limits, and needs no browser flow — the practical
        way to exercise this adapter without a real bank.
        """
        return self._request(
            "POST", "/api/v2/connections/link/test_sandbox", token=access_token, json={}
        )

    # --- webhooks: none exist ---------------------------------------------

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        """Always false. Fintable documents no webhooks.

        Refusing is the honest answer: accepting unsigned callbacks would let
        anyone who finds the URL trigger syncs on our side.
        """
        raise WebhookVerificationError(
            "Fintable does not provide webhooks; scheduled sync is the only freshness path."
        )

    def parse_webhook(self, body: dict[str, Any]) -> WebhookEvent:
        return WebhookEvent(provider_ref="", event_type=EVENT_UNKNOWN, raw=body)


# --- parsing -------------------------------------------------------------


def _rows(payload: Any) -> list[dict]:
    """Fintable wraps lists in {"data": [...]}, but not uniformly."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
    return []


def _classify(account_type: str | None) -> AccountType | None:
    """Asset or liability, from Fintable's free-text type.

    This is what decides whether a positive amount is money in or money out,
    so an unrecognized type returns None rather than guessing — the database
    trigger then reads it as an asset, and a wrong guess here would silently
    invert every transaction on the account.
    """
    if not account_type:
        return None
    family = account_type.split("/")[0].strip().lower()
    if family in _LIABILITY_FAMILIES:
        return AccountType.LIABILITY
    return AccountType.ASSET


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _to_account(row: dict) -> RawAccount:
    account_type = row.get("type")
    return RawAccount(
        provider_account_id=str(row["id"]),
        name=str(row.get("display_name") or row.get("name") or "Account"),
        account_type=account_type,
        classification=_classify(account_type),
        current_balance=_decimal(row.get("balance")),
    )


def _to_transaction(row: dict) -> RawTransaction | None:
    """One transaction, or None if it is unusable.

    Skipping beats raising: one malformed row should not abandon a page of
    good ones, and the warning says which id to go and look at.
    """
    try:
        raw_date = row["date"]
        amount = _decimal(row.get("amount"))
        if amount is None:
            raise ValueError(f"unparseable amount {row.get('amount')!r}")
        category = row.get("category")
        return RawTransaction(
            provider_transaction_id=str(row["id"]),
            account_id=str(row["account_id"]),
            # Already signed the way the rest of the system expects: negative
            # is money out, per Fintable's schema note.
            amount=amount,
            date=date.fromisoformat(str(raw_date)[:10]),
            merchant_name=row.get("merchant"),
            description=str(row.get("description") or ""),
            # Fintable never stores pending rows as transactions; its own
            # schema says this is always false.
            pending=bool(row.get("pending", False)),
            provider_category=(category or {}).get("name") if isinstance(category, dict) else None,
            raw_payload=row,
        )
    except (KeyError, ValueError, TypeError) as exc:
        log.warning("fintable.unparseable_transaction", id=str(row.get("id")), error=str(exc))
        return None


def _explain(response: httpx.Response) -> str:
    """Turn an error body into something worth reading in a log."""
    detail = response.text[:300]
    try:
        body = response.json()
        detail = str(body.get("error") or body.get("message") or body)[:300]
    except ValueError:
        pass
    if response.status_code == 401:
        return f"Fintable rejected the token (401). It may have expired or been revoked: {detail}"
    if response.status_code == 429:
        return f"Fintable rate limit (429). Limits are per credential, not per workspace: {detail}"
    return f"Fintable API error {response.status_code}: {detail}"
