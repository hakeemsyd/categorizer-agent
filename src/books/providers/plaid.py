"""Plaid adapter — the only module in the codebase that imports ``plaid``.

Plaid SDK imports are deliberately lazy so that the test suite (which runs
against ``FakeProvider``) never needs the vendor package loaded.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from books.config import get_settings
from books.core.accounting import AccountType
from books.core.errors import ConfigurationError, ProviderError, WebhookVerificationError
from books.logging import get_logger
from books.providers.base import (
    EVENT_ITEM_ERROR,
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

# Plaid webhook codes -> our normalized vocabulary.
_WEBHOOK_CODES = {
    ("TRANSACTIONS", "SYNC_UPDATES_AVAILABLE"): EVENT_SYNC_AVAILABLE,
    ("TRANSACTIONS", "DEFAULT_UPDATE"): EVENT_SYNC_AVAILABLE,
    ("TRANSACTIONS", "INITIAL_UPDATE"): EVENT_SYNC_AVAILABLE,
    ("TRANSACTIONS", "HISTORICAL_UPDATE"): EVENT_SYNC_AVAILABLE,
    ("ITEM", "ERROR"): EVENT_ITEM_ERROR,
    ("ITEM", "PENDING_EXPIRATION"): EVENT_ITEM_ERROR,
    ("ITEM", "USER_PERMISSION_REVOKED"): EVENT_ITEM_REVOKED,
}

# Plaid account types that represent money owed rather than money held.
_LIABILITY_TYPES = {"credit", "loan"}


class PlaidProvider:
    """Implements :class:`books.providers.base.TransactionProvider`."""

    name = "plaid"

    def __init__(self) -> None:
        self._settings = get_settings()
        self._api: Any | None = None
        self._verification_keys: dict[str, dict[str, Any]] = {}

    # --- client ---------------------------------------------------------

    def _client(self) -> Any:
        if self._api is not None:
            return self._api

        import plaid
        from plaid.api import plaid_api

        s = self._settings
        if not s.plaid_client_id or not s.plaid_secret:
            raise ConfigurationError("BOOKS_PLAID_CLIENT_ID / BOOKS_PLAID_SECRET are required.")

        host = (
            plaid.Environment.Sandbox if s.plaid_env == "sandbox" else plaid.Environment.Production
        )
        config = plaid.Configuration(
            host=host,
            api_key={"clientId": s.plaid_client_id, "secret": s.plaid_secret},
        )
        self._api = plaid_api.PlaidApi(plaid.ApiClient(config))
        return self._api

    @staticmethod
    def _wrap(exc: Exception, operation: str) -> ProviderError:
        body = getattr(exc, "body", None)
        return ProviderError(f"Plaid {operation} failed: {body or exc}")

    # --- link -----------------------------------------------------------

    def create_link_token(self, user_ref: str) -> LinkToken:
        from plaid.model.country_code import CountryCode
        from plaid.model.link_token_create_request import LinkTokenCreateRequest
        from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
        from plaid.model.products import Products

        kwargs: dict[str, Any] = {
            "client_name": "Books",
            "language": "en",
            "country_codes": [CountryCode("US")],
            "user": LinkTokenCreateRequestUser(client_user_id=user_ref),
            "products": [Products("transactions")],
        }
        if self._settings.plaid_webhook_url:
            kwargs["webhook"] = self._settings.plaid_webhook_url
        if self._settings.plaid_redirect_uri:
            kwargs["redirect_uri"] = self._settings.plaid_redirect_uri

        try:
            response = self._client().link_token_create(LinkTokenCreateRequest(**kwargs))
        except Exception as exc:
            raise self._wrap(exc, "link_token_create") from exc
        return LinkToken(token=response["link_token"], expiration=str(response["expiration"]))

    def exchange_public_token(self, public_token: str) -> ItemCredentials:
        from plaid.model.item_get_request import ItemGetRequest
        from plaid.model.item_public_token_exchange_request import (
            ItemPublicTokenExchangeRequest,
        )

        try:
            exchanged = self._client().item_public_token_exchange(
                ItemPublicTokenExchangeRequest(public_token=public_token)
            )
            access_token = exchanged["access_token"]
            item = self._client().item_get(ItemGetRequest(access_token=access_token))
        except Exception as exc:
            raise self._wrap(exc, "item_public_token_exchange") from exc

        institution_id = item["item"].get("institution_id")
        return ItemCredentials(
            provider_item_id=item["item"]["item_id"],
            access_token=access_token,
            institution_name=self._institution_name(institution_id),
        )

    def _institution_name(self, institution_id: str | None) -> str:
        if not institution_id:
            return "Unknown institution"
        from plaid.model.country_code import CountryCode
        from plaid.model.institutions_get_by_id_request import InstitutionsGetByIdRequest

        try:
            response = self._client().institutions_get_by_id(
                InstitutionsGetByIdRequest(
                    institution_id=institution_id, country_codes=[CountryCode("US")]
                )
            )
            return str(response["institution"]["name"])
        except Exception:  # institution lookup is cosmetic — never fail a link over it
            log.warning("plaid.institution_lookup_failed", institution_id=institution_id)
            return institution_id

    # --- sync -----------------------------------------------------------

    def sync_transactions(self, access_token: str, cursor: str | None) -> SyncResult:
        from plaid.model.transactions_sync_request import TransactionsSyncRequest

        kwargs: dict[str, Any] = {"access_token": access_token, "count": 500}
        if cursor:
            kwargs["cursor"] = cursor

        try:
            response = self._client().transactions_sync(TransactionsSyncRequest(**kwargs))
        except Exception as exc:
            raise self._wrap(exc, "transactions_sync") from exc

        return SyncResult(
            added=[self._to_transaction(t) for t in response["added"]],
            modified=[self._to_transaction(t) for t in response["modified"]],
            removed=[t["transaction_id"] for t in response["removed"]],
            next_cursor=response["next_cursor"],
            has_more=bool(response["has_more"]),
        )

    @staticmethod
    def _to_transaction(tx: Any) -> RawTransaction:
        payload = tx.to_dict() if hasattr(tx, "to_dict") else dict(tx)
        # Plaid reports money out as a positive number; we store signed amounts
        # where negative means money left the account (plan.md §4).
        amount = -Decimal(str(payload["amount"]))

        tx_date = _to_date(payload.get("date"))

        personal = payload.get("personal_finance_category") or {}
        provider_category = personal.get("detailed") or personal.get("primary")
        if not provider_category and payload.get("category"):
            provider_category = " > ".join(payload["category"])

        return RawTransaction(
            provider_transaction_id=payload["transaction_id"],
            account_id=payload["account_id"],
            amount=amount,
            date=tx_date,
            merchant_name=payload.get("merchant_name") or payload.get("name"),
            description=payload.get("original_description") or payload.get("name") or "",
            pending=bool(payload.get("pending")),
            provider_category=provider_category,
            raw_payload=_jsonable(payload),
        )

    def get_accounts(self, access_token: str) -> list[RawAccount]:
        from plaid.model.accounts_get_request import AccountsGetRequest

        try:
            response = self._client().accounts_get(AccountsGetRequest(access_token=access_token))
        except Exception as exc:
            raise self._wrap(exc, "accounts_get") from exc

        accounts: list[RawAccount] = []
        for account in response["accounts"]:
            data = account.to_dict() if hasattr(account, "to_dict") else dict(account)
            account_type = str(data.get("type") or "")
            balances = data.get("balances") or {}
            current = balances.get("current")
            accounts.append(
                RawAccount(
                    provider_account_id=data["account_id"],
                    name=data.get("official_name") or data.get("name") or "Account",
                    account_type=f"{account_type}/{data.get('subtype')}".strip("/"),
                    classification=(
                        AccountType.LIABILITY
                        if account_type in _LIABILITY_TYPES
                        else AccountType.ASSET
                    ),
                    current_balance=Decimal(str(current)) if current is not None else None,
                )
            )
        return accounts

    def force_refresh(self, access_token: str) -> None:
        from plaid.model.transactions_refresh_request import TransactionsRefreshRequest

        try:
            self._client().transactions_refresh(
                TransactionsRefreshRequest(access_token=access_token)
            )
        except Exception as exc:
            raise self._wrap(exc, "transactions_refresh") from exc

    # --- webhooks -------------------------------------------------------

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        """Verify Plaid's ``Plaid-Verification`` JWT and the body SHA-256.

        Plaid signs each webhook with an ES256 JWT whose ``request_body_sha256``
        claim pins the exact bytes we received.
        """
        import jwt

        token = _header(headers, "plaid-verification")
        if not token:
            raise WebhookVerificationError("Missing Plaid-Verification header")

        try:
            kid = jwt.get_unverified_header(token).get("kid")
            if not kid:
                raise WebhookVerificationError("Plaid-Verification JWT has no kid")
            key = jwt.PyJWK(self._verification_key(kid)).key
            claims = jwt.decode(token, key=key, algorithms=["ES256"])
        except WebhookVerificationError:
            raise
        except Exception as exc:
            raise WebhookVerificationError(f"Plaid webhook JWT rejected: {exc}") from exc

        expected = claims.get("request_body_sha256", "")
        if not hmac.compare_digest(expected, hashlib.sha256(body).hexdigest()):
            raise WebhookVerificationError("Plaid webhook body hash mismatch")
        return True

    def _verification_key(self, kid: str) -> dict[str, Any]:
        if kid in self._verification_keys:
            return self._verification_keys[kid]

        from plaid.model.webhook_verification_key_get_request import (
            WebhookVerificationKeyGetRequest,
        )

        try:
            response = self._client().webhook_verification_key_get(
                WebhookVerificationKeyGetRequest(key_id=kid)
            )
        except Exception as exc:
            raise WebhookVerificationError(
                f"Could not fetch Plaid webhook key {kid}: {exc}"
            ) from exc

        key = response["key"]
        key = key.to_dict() if hasattr(key, "to_dict") else dict(key)
        jwk = {k: v for k, v in key.items() if k in {"kty", "crv", "x", "y", "kid", "use", "alg"}}
        self._verification_keys[kid] = jwk
        return jwk

    def parse_webhook(self, body: dict[str, Any]) -> WebhookEvent:
        webhook_type = str(body.get("webhook_type", ""))
        webhook_code = str(body.get("webhook_code", ""))
        return WebhookEvent(
            provider_item_id=str(body.get("item_id", "")),
            event_type=_WEBHOOK_CODES.get((webhook_type, webhook_code), EVENT_UNKNOWN),
            raw=body,
        )


def _to_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    raise ProviderError(f"Plaid transaction has an unusable date: {value!r}")


def _header(headers: dict[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def _jsonable(value: Any) -> Any:
    """Plaid models contain date/Decimal objects; JSONB needs plain types."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "to_dict"):
        return _jsonable(value.to_dict())
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)
