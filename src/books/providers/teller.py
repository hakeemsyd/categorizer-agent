"""Teller adapter (https://teller.io/docs) — the only module that imports its SDK shape.

Teller's model is closer to Plaid's than Fintable's was, but not identical:

* **Two-part authentication.** Every call needs mutual TLS (a client
  certificate + private key issued by Teller for this application) *and*
  HTTP Basic Auth using the enrollment's access token as the username with an
  empty password. Sandbox is the one exception — Teller's docs say it "does
  not require client certificates but accepts them if provided," so local
  development needs no certificate at all.
* **Teller Connect hands the browser the final access token directly.**
  Unlike Plaid's public-token-then-server-exchange dance, Connect's
  ``onSuccess`` callback already carries the real, usable access token. There
  is nothing to "exchange" — `exchange_public_token` here just uses the token
  to fetch the enrollment's accounts and confirm it actually works, rather
  than trusting whatever institution name the browser claims.
* **No delta-sync primitive and no `updated_at`.** Transactions are paginated
  per-account with `count`/`from_id`/date filters — there is no signal for
  "what changed since I last looked," so `sync_transactions` re-walks a
  trailing window (`TELLER_SYNC_OVERLAP_DAYS`) on every incremental sync to
  catch a `pending` transaction that later posts under a different id.
  Deletions are never reported, for the same reason — a real limitation of
  the API, not a gap in this adapter.
* **No refresh endpoint, because there is nothing to refresh.** Teller's docs
  describe balances as "live, real-time," and no force-refresh endpoint
  appears anywhere in the API reference. `force_refresh` re-reads `/accounts`
  instead — cheap, and it surfaces a revoked enrollment early.
* **Real webhooks**, with a documented HMAC-SHA256 signature scheme. One
  payload shape (`enrollment.disconnected`) is fully documented; the
  `transactions.processed` example in Teller's docs did not render a full
  JSON body when fetched, so `payload.enrollment_id` there is an assumption,
  not a confirmed fact — see `parse_webhook`. If it turns out wrong, the
  webhook route's existing "unknown item, ignored" handling means the only
  cost is a missed fast path; Celery Beat's hourly sync still catches it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import httpx

from books.config import get_settings
from books.core.accounting import AccountType
from books.core.errors import ConfigurationError, ProviderError, WebhookVerificationError
from books.logging import get_logger
from books.providers.base import (
    EVENT_ITEM_REVOKED,
    EVENT_SYNC_AVAILABLE,
    EVENT_UNKNOWN,
    ItemCredentials,
    LinkToken,
    RawAccount,
    RawTransaction,
    SyncResult,
    WebhookEvent,
)

log = get_logger(__name__)

# How far back an incremental sync re-walks, since Teller has no updated_since
# or delta signal. Wide enough to catch a pending transaction that later posts
# under a different id; a documented judgment call, not a Teller-specified
# value. Duplicates are harmless — ingestion upserts by provider_transaction_id
# and never overwrites a human-set category.
TELLER_SYNC_OVERLAP_DAYS = 10

# Webhook replay protection, per Teller's docs.
WEBHOOK_MAX_AGE_SECONDS = 180

_LIABILITY_TYPES = {"credit"}

_EVENT_TYPES = {
    "enrollment.disconnected": EVENT_ITEM_REVOKED,
    "transactions.processed": EVENT_SYNC_AVAILABLE,
    "account.number_verification.processed": EVENT_UNKNOWN,
    "webhook.test": EVENT_UNKNOWN,
}


@dataclass(frozen=True)
class _Cursor:
    """What our single opaque ``cursor`` string encodes.

    One access token can cover several accounts (Teller Connect lets a user
    share more than one), and Teller paginates *within* one account at a
    time — so the cursor has to track both "which accounts are left" and
    "how far into the current one," plus the incremental watermark once a
    full walk finishes.
    """

    account_ids: list[str] | None  # None = "start a fresh walk from /accounts"
    from_id: str | None  # Teller's own within-account pagination cursor
    since: str | None  # ISO date watermark for the next incremental walk

    def encode(self) -> str:
        return json.dumps(
            {"accounts": self.account_ids, "from_id": self.from_id, "since": self.since}
        )

    @classmethod
    def decode(cls, raw: str | None) -> _Cursor:
        if not raw:
            return cls(account_ids=None, from_id=None, since=None)
        parsed = json.loads(raw)
        return cls(
            account_ids=parsed.get("accounts"),
            from_id=parsed.get("from_id"),
            since=parsed.get("since"),
        )


class TellerProvider:
    """Implements :class:`books.providers.base.TransactionProvider`."""

    name = "teller"

    def __init__(self) -> None:
        self._settings = get_settings()
        self._client: httpx.Client | None = None

    # --- HTTP plumbing ---------------------------------------------------

    def _http(self) -> httpx.Client:
        if self._client is not None:
            return self._client

        cert = None
        if self._settings.teller_cert_path and self._settings.teller_key_path:
            cert = (self._settings.teller_cert_path, self._settings.teller_key_path)
        elif self._settings.teller_environment != "sandbox":
            raise ConfigurationError(
                "BOOKS_TELLER_CERT_PATH and BOOKS_TELLER_KEY_PATH are required outside "
                "the sandbox environment — Teller authenticates API callers with mutual TLS."
            )

        self._client = httpx.Client(
            base_url=self._settings.teller_base_url, cert=cert, timeout=30.0
        )
        return self._client

    def _request(
        self, method: str, path: str, *, access_token: str, params: dict[str, Any] | None = None
    ) -> Any:
        try:
            response = self._http().request(
                method, path, params=params, auth=httpx.BasicAuth(access_token, "")
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = _error_detail(exc.response)
            status = exc.response.status_code
            raise ProviderError(f"Teller {method} {path} failed ({status}): {detail}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"Teller {method} {path} failed: {exc}") from exc
        return response.json() if response.content else None

    # --- link: Connect hands us the real token directly -------------------

    def create_link_token(self, user_ref: str) -> LinkToken:
        """No network call: Connect only needs the public application id.

        Unlike Plaid, Teller does not mint a per-session server-side token —
        `applicationId` is a fixed, public value from the Teller Dashboard
        that the browser embeds directly.
        """
        if not self._settings.teller_application_id:
            raise ConfigurationError("BOOKS_TELLER_APPLICATION_ID is required.")
        return LinkToken(token=self._settings.teller_application_id, expiration="")

    def exchange_public_token(self, public_token: str) -> ItemCredentials:
        """``public_token`` is already Teller's real access token here.

        Connect's ``onSuccess`` hands the browser the finished credential —
        there is no separate exchange step. What we do here instead is prove
        the token actually works and learn the institution name and
        enrollment id server-side, rather than trusting the browser's claim.
        """
        accounts = self._list_accounts(public_token)
        if not accounts:
            raise ProviderError("Teller Connect finished but the enrollment has no accounts.")

        first = accounts[0]
        institution = first.get("institution") or {}
        return ItemCredentials(
            provider_item_id=first["enrollment_id"],
            access_token=public_token,
            institution_name=institution.get("name") or "Unknown institution",
        )

    # --- accounts ----------------------------------------------------------

    def _list_accounts(self, access_token: str) -> list[dict[str, Any]]:
        return self._request("GET", "/accounts", access_token=access_token) or []

    def get_accounts(self, access_token: str) -> list[RawAccount]:
        return [self._to_account(access_token, data) for data in self._list_accounts(access_token)]

    def _to_account(self, access_token: str, data: dict[str, Any]) -> RawAccount:
        return RawAccount(
            provider_account_id=data["id"],
            name=data.get("name") or "Account",
            account_type=f"{data.get('type')}/{data.get('subtype')}",
            classification=(
                AccountType.LIABILITY if data.get("type") in _LIABILITY_TYPES else AccountType.ASSET
            ),
            current_balance=self._fetch_balance(access_token, data["id"]),
        )

    def _fetch_balance(self, access_token: str, account_id: str) -> Decimal | None:
        body = self._request("GET", f"/accounts/{account_id}/balances", access_token=access_token)
        if not body:
            return None
        # "At least one balance (ledger or available) is always provided."
        value = body.get("ledger") if body.get("ledger") is not None else body.get("available")
        return Decimal(str(value)) if value is not None else None

    # --- transactions -----------------------------------------------------

    def sync_transactions(self, access_token: str, cursor: str | None) -> SyncResult:
        state = _Cursor.decode(cursor)
        # `since` is set once and carried unchanged through every page of an
        # incremental walk (it only changes when a fresh resting cursor is
        # written at the very end) — so this alone distinguishes "the very
        # first sync ever" from "any page of an incremental one," regardless
        # of whether this particular call is starting fresh or continuing.
        is_incremental = state.since is not None

        if state.account_ids is None:
            account_ids = [a["id"] for a in self._list_accounts(access_token)]
            state = _Cursor(account_ids=account_ids, from_id=None, since=state.since)

        if not state.account_ids:
            return SyncResult(added=[], modified=[], removed=[], next_cursor=cursor, has_more=False)

        current_account = state.account_ids[0]
        params: dict[str, Any] = {}
        if state.since:
            # Kept alongside from_id (not just on the first page) — Teller's
            # docs don't confirm how the two compose, and dropping the date
            # bound on later pages risks silently walking past it.
            params["start_date"] = state.since
        if state.from_id:
            params["from_id"] = state.from_id

        rows = (
            self._request(
                "GET",
                f"/accounts/{current_account}/transactions",
                access_token=access_token,
                params=params,
            )
            or []
        )
        parsed = [self._to_transaction(current_account, r) for r in rows]

        if rows:
            next_state = _Cursor(
                account_ids=state.account_ids, from_id=rows[-1]["id"], since=state.since
            )
            return SyncResult(
                added=[] if is_incremental else parsed,
                modified=parsed if is_incremental else [],
                removed=[],  # Teller reports no deletions — see module docstring.
                next_cursor=next_state.encode(),
                has_more=True,
            )

        # This account is drained. Move on, or — if that was the last one —
        # rest with a fresh watermark for the next incremental sync.
        remaining = state.account_ids[1:]
        if remaining:
            next_state = _Cursor(account_ids=remaining, from_id=None, since=state.since)
            return SyncResult(
                added=[], modified=[], removed=[], next_cursor=next_state.encode(), has_more=True
            )

        watermark = (date.today() - timedelta(days=TELLER_SYNC_OVERLAP_DAYS)).isoformat()
        resting = _Cursor(account_ids=None, from_id=None, since=watermark)
        return SyncResult(
            added=[], modified=[], removed=[], next_cursor=resting.encode(), has_more=False
        )

    @staticmethod
    def _to_transaction(account_id: str, data: dict[str, Any]) -> RawTransaction:
        details = data.get("details") or {}
        counterparty = details.get("counterparty") or {}
        return RawTransaction(
            provider_transaction_id=data["id"],
            account_id=account_id,
            amount=Decimal(str(data["amount"])),  # already signed: negative = money out
            date=date.fromisoformat(data["date"]),
            merchant_name=counterparty.get("name"),
            description=data.get("description") or "",
            pending=data.get("status") == "pending",
            provider_category=details.get("category"),
            raw_payload=data,
        )

    # --- refresh: nothing to trigger, reads are already live --------------

    def force_refresh(self, access_token: str) -> None:
        """Teller documents no refresh endpoint — balances and transactions
        are described as live, real-time reads. Re-fetching accounts is not a
        no-op, though: it's a cheap way to surface a revoked or broken
        enrollment (a 401/403) before the next scheduled sync would.
        """
        self._list_accounts(access_token)

    # --- webhooks: real, documented, and actually verified ----------------

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        secret = self._settings.teller_signing_secret
        if not secret:
            raise ConfigurationError("BOOKS_TELLER_SIGNING_SECRET is required to verify webhooks.")

        header = _header(headers, "teller-signature")
        if not header:
            raise WebhookVerificationError("Missing Teller-Signature header")

        parts: dict[str, list[str]] = {}
        for piece in header.split(","):
            key, _, value = piece.partition("=")
            parts.setdefault(key.strip(), []).append(value.strip())

        timestamps = parts.get("t")
        signatures = parts.get("v1")
        if not timestamps or not signatures:
            raise WebhookVerificationError("Teller-Signature header is malformed")
        timestamp = timestamps[0]

        try:
            age = time.time() - int(timestamp)
        except ValueError as exc:
            raise WebhookVerificationError("Teller-Signature timestamp is not an integer") from exc
        if age > WEBHOOK_MAX_AGE_SECONDS:
            raise WebhookVerificationError(
                f"Teller webhook is {age:.0f}s old, older than the {WEBHOOK_MAX_AGE_SECONDS}s "
                "replay window"
            )

        expected = hmac.new(
            secret.encode(), f"{timestamp}.{body.decode()}".encode(), hashlib.sha256
        ).hexdigest()
        if not any(hmac.compare_digest(expected, given) for given in signatures):
            raise WebhookVerificationError("Teller webhook signature did not match")
        return True

    def parse_webhook(self, body: dict[str, Any]) -> WebhookEvent:
        payload = body.get("payload") or {}
        return WebhookEvent(
            # Confirmed for enrollment.disconnected; assumed (not confirmed)
            # for transactions.processed — see the module docstring.
            provider_item_id=str(payload.get("enrollment_id", "")),
            event_type=_EVENT_TYPES.get(str(body.get("type")), EVENT_UNKNOWN),
            raw=body,
        )


def _header(headers: dict[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
        error = body.get("error") or {}
        return f"{error.get('code')}: {error.get('message')}" if error else str(body)
    except ValueError:
        return response.text[:300]
