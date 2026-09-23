"""TellerProvider against a mocked transport — no network, no real certificate.

Every response shape here matches Teller's own documentation (see the session
that added this adapter): the accounts/balances/transactions field names, the
`{"error": {"code", "message"}}` error envelope, and the `Teller-Signature:
t=...,v1=...` webhook scheme. Nothing here is guessed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable
from decimal import Decimal

import httpx
import pytest

from books.core.accounting import AccountType
from books.core.errors import ConfigurationError, ProviderError, WebhookVerificationError
from books.providers.base import EVENT_ITEM_REVOKED, EVENT_SYNC_AVAILABLE, EVENT_UNKNOWN
from books.providers.teller import TellerProvider

BASE_URL = "https://api.teller.io"
ACCESS_TOKEN = "token_abc123"


def make_provider(handler: Callable[[httpx.Request], httpx.Response]) -> TellerProvider:
    """A provider whose HTTP client is wired to a mock transport.

    Bypasses the real settings-driven client construction — no certificate or
    application id is needed, and no network call ever happens.
    """
    provider = TellerProvider()
    provider._client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)
    return provider


def json_response(status_code: int, payload: dict | list) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def _basic_auth_username(request: httpx.Request) -> str:
    """Teller authenticates with the access token as a Basic Auth username."""
    scheme, _, encoded = request.headers["authorization"].partition(" ")
    assert scheme == "Basic"
    import base64

    decoded = base64.b64decode(encoded).decode()
    username, _, password = decoded.partition(":")
    assert password == ""
    return username


CHECKING = {
    "id": "acc_checking",
    "name": "Business Checking",
    "type": "depository",
    "subtype": "checking",
    "enrollment_id": "enr_1",
    "institution": {"id": "chase", "name": "Chase"},
}
CARD = {
    "id": "acc_card",
    "name": "Rewards Card",
    "type": "credit",
    "subtype": "credit_card",
    "enrollment_id": "enr_1",
    "institution": {"id": "chase", "name": "Chase"},
}


# --- link: Connect already hands us the real token ---------------------


def test_create_link_token_needs_no_network_call(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("minting a link token must not touch the network")

    provider = make_provider(handler)
    provider._settings.teller_application_id = "app_123"
    token = provider.create_link_token("tenant-1")
    assert token.token == "app_123"


def test_create_link_token_without_an_application_id_is_a_configuration_error() -> None:
    provider = TellerProvider()
    provider._settings.teller_application_id = None
    with pytest.raises(ConfigurationError, match="APPLICATION_ID"):
        provider.create_link_token("tenant-1")


def test_exchange_confirms_the_token_and_learns_the_enrollment_server_side() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/accounts"
        assert _basic_auth_username(request) == ACCESS_TOKEN
        return json_response(200, [CHECKING, CARD])

    credentials = make_provider(handler).exchange_public_token(ACCESS_TOKEN)

    assert credentials.provider_item_id == "enr_1"
    assert credentials.access_token == ACCESS_TOKEN
    assert credentials.institution_name == "Chase"


def test_exchange_with_no_accounts_is_a_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(200, [])

    with pytest.raises(ProviderError, match="no accounts"):
        make_provider(handler).exchange_public_token(ACCESS_TOKEN)


# --- accounts and balances -----------------------------------------------


def test_get_accounts_classifies_credit_as_liability_and_fetches_balances() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/accounts":
            return json_response(200, [CHECKING, CARD])
        if request.url.path == "/accounts/acc_checking/balances":
            return json_response(
                200, {"account_id": "acc_checking", "ledger": "18422.31", "available": "18000.00"}
            )
        if request.url.path == "/accounts/acc_card/balances":
            return json_response(
                200, {"account_id": "acc_card", "ledger": None, "available": "-412.10"}
            )
        raise AssertionError(request.url.path)

    accounts = make_provider(handler).get_accounts(ACCESS_TOKEN)

    checking = next(a for a in accounts if a.provider_account_id == "acc_checking")
    card = next(a for a in accounts if a.provider_account_id == "acc_card")
    assert checking.classification is AccountType.ASSET
    assert checking.current_balance == Decimal("18422.31")  # ledger preferred over available
    assert card.classification is AccountType.LIABILITY
    assert card.current_balance == Decimal("-412.10")  # falls back to available when ledger is null


# --- transactions: the multi-account, multi-page walk ----------------------


def _transaction(tx_id: str, amount: str, day: str, *, pending: bool = False) -> dict:
    return {
        "id": tx_id,
        "account_id": "unused-overwritten-by-caller",
        "date": day,
        "amount": amount,
        "description": "COFFEE SHOP",
        "status": "pending" if pending else "posted",
        "details": {"category": "dining", "counterparty": {"name": "Blue Bottle"}},
    }


def test_a_full_first_sync_walks_every_account_then_rests_with_a_watermark() -> None:
    """acc_checking: one page of data, then an empty terminator page.
    acc_card: same shape. Every row must land in `added` (first sync ever)."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/accounts":
            return json_response(200, [CHECKING, CARD])

        calls.append((request.url.path, dict(request.url.params)))
        account_id = request.url.path.split("/")[2]
        has_from_id = "from_id" in request.url.params
        assert "start_date" not in request.url.params  # first sync: no watermark yet

        if account_id == "acc_checking" and not has_from_id:
            return json_response(200, [_transaction("t1", "-4.50", "2026-07-24")])
        if account_id == "acc_card" and not has_from_id:
            return json_response(200, [_transaction("t2", "-9.00", "2026-07-25")])
        return json_response(200, [])  # terminator page for either account

    provider = make_provider(handler)
    results = []
    cursor = None
    for _ in range(10):
        result = provider.sync_transactions(ACCESS_TOKEN, cursor)
        results.append(result)
        cursor = result.next_cursor
        if not result.has_more:
            break
    else:  # pragma: no cover
        raise AssertionError("sync never settled")

    all_added = [t for r in results for t in r.added]
    all_modified = [t for r in results for t in r.modified]
    assert {t.provider_transaction_id for t in all_added} == {"t1", "t2"}
    assert all_modified == []  # first sync: nothing is "modified"
    assert results[-1].has_more is False

    final_cursor = json.loads(results[-1].next_cursor)
    assert final_cursor["accounts"] is None  # resting: ready to refetch accounts next time
    assert final_cursor["since"] is not None  # a watermark was recorded


def test_incremental_sync_applies_the_watermark_and_lands_in_modified() -> None:
    resting = json.dumps({"accounts": None, "from_id": None, "since": "2026-07-15"})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/accounts":
            return json_response(200, [CHECKING])
        assert request.url.params["start_date"] == "2026-07-15"
        return json_response(200, [_transaction("t3", "-1.00", "2026-07-26")])

    result = make_provider(handler).sync_transactions(ACCESS_TOKEN, resting)

    assert [t.provider_transaction_id for t in result.modified] == ["t3"]
    assert result.added == []


def test_continuing_an_incremental_walk_still_lands_in_modified() -> None:
    """Regression: a bug here classified continuation pages of an incremental
    walk as `added` because it also checked `account_ids is None`, which is
    false once a walk is under way — only `since` reliably marks "incremental"
    across every page, not just the first.
    """
    mid_walk = json.dumps({"accounts": ["acc_checking"], "from_id": "t3", "since": "2026-07-15"})

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["from_id"] == "t3"
        assert request.url.params["start_date"] == "2026-07-15"
        return json_response(200, [_transaction("t4", "-2.00", "2026-07-27")])

    result = make_provider(handler).sync_transactions(ACCESS_TOKEN, mid_walk)

    assert [t.provider_transaction_id for t in result.modified] == ["t4"]
    assert result.added == []


def test_transactions_never_report_a_removal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/accounts":
            return json_response(200, [CHECKING])
        return json_response(200, [])

    result = make_provider(handler).sync_transactions(ACCESS_TOKEN, None)
    assert result.removed == []


def test_pending_status_and_amount_sign_are_carried_through() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/accounts":
            return json_response(200, [CHECKING])
        return json_response(200, [_transaction("t5", "-4.50", "2026-07-24", pending=True)])

    result = make_provider(handler).sync_transactions(ACCESS_TOKEN, None)
    row = result.added[0]
    assert row.pending is True
    assert row.amount == Decimal("-4.50")
    assert row.merchant_name == "Blue Bottle"
    assert row.provider_category == "dining"


# --- force refresh: no endpoint exists, so it re-reads accounts ------------


def test_force_refresh_re_reads_accounts_rather_than_doing_nothing() -> None:
    called = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(request.url.path)
        return json_response(200, [CHECKING])

    make_provider(handler).force_refresh(ACCESS_TOKEN)
    assert called == ["/accounts"]


# --- webhooks: real signature verification ---------------------------------


def _sign(secret: str, timestamp: int, body: bytes) -> str:
    return hmac.new(
        secret.encode(), f"{timestamp}.{body.decode()}".encode(), hashlib.sha256
    ).hexdigest()


def test_a_correctly_signed_webhook_is_accepted() -> None:
    provider = TellerProvider()
    provider._settings.teller_signing_secret = "whsec_test"
    body = json.dumps({"type": "enrollment.disconnected"}).encode()
    now = int(time.time())
    signature = _sign("whsec_test", now, body)

    assert provider.verify_webhook({"Teller-Signature": f"t={now},v1={signature}"}, body) is True


def test_a_tampered_body_is_rejected() -> None:
    provider = TellerProvider()
    provider._settings.teller_signing_secret = "whsec_test"
    now = int(time.time())
    signature = _sign("whsec_test", now, b'{"type": "enrollment.disconnected"}')
    tampered_body = b'{"type": "webhook.test"}'

    with pytest.raises(WebhookVerificationError, match="did not match"):
        provider.verify_webhook({"Teller-Signature": f"t={now},v1={signature}"}, tampered_body)


def test_an_old_signature_is_rejected_as_a_replay() -> None:
    provider = TellerProvider()
    provider._settings.teller_signing_secret = "whsec_test"
    body = b'{"type": "enrollment.disconnected"}'
    old = int(time.time()) - 300  # older than the 180s window
    signature = _sign("whsec_test", old, body)

    with pytest.raises(WebhookVerificationError, match="replay"):
        provider.verify_webhook({"Teller-Signature": f"t={old},v1={signature}"}, body)


def test_a_missing_header_is_rejected() -> None:
    provider = TellerProvider()
    provider._settings.teller_signing_secret = "whsec_test"
    with pytest.raises(WebhookVerificationError, match="Missing"):
        provider.verify_webhook({}, b"{}")


def test_verification_without_a_configured_secret_is_a_configuration_error() -> None:
    provider = TellerProvider()
    provider._settings.teller_signing_secret = None
    with pytest.raises(ConfigurationError):
        provider.verify_webhook({"Teller-Signature": "t=1,v1=x"}, b"{}")


def test_parse_webhook_maps_confirmed_event_types() -> None:
    provider = TellerProvider()
    event = provider.parse_webhook(
        {"type": "enrollment.disconnected", "payload": {"enrollment_id": "enr_1", "reason": "x"}}
    )
    assert event.event_type == EVENT_ITEM_REVOKED
    assert event.provider_item_id == "enr_1"


def test_parse_webhook_maps_the_sync_trigger_event() -> None:
    provider = TellerProvider()
    event = provider.parse_webhook(
        {"type": "transactions.processed", "payload": {"enrollment_id": "enr_1"}}
    )
    assert event.event_type == EVENT_SYNC_AVAILABLE


def test_parse_webhook_falls_back_to_unknown_for_anything_else() -> None:
    provider = TellerProvider()
    assert (
        provider.parse_webhook({"type": "webhook.test", "payload": {}}).event_type == EVENT_UNKNOWN
    )
    assert (
        provider.parse_webhook({"type": "something.new", "payload": {}}).event_type == EVENT_UNKNOWN
    )


# --- error handling ----------------------------------------------------------


def test_an_http_error_uses_tellers_error_envelope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(401, {"error": {"code": "unauthorized", "message": "Bad token"}})

    with pytest.raises(ProviderError, match="unauthorized: Bad token"):
        make_provider(handler).get_accounts(ACCESS_TOKEN)


# --- mTLS enforcement outside sandbox ---------------------------------------


def test_a_missing_certificate_outside_sandbox_is_a_configuration_error() -> None:
    provider = TellerProvider()
    provider._settings.teller_environment = "production"
    provider._settings.teller_cert_path = None
    provider._settings.teller_key_path = None
    with pytest.raises(ConfigurationError, match="mutual TLS"):
        provider._http()


def test_sandbox_needs_no_certificate() -> None:
    provider = TellerProvider()
    provider._settings.teller_environment = "sandbox"
    provider._settings.teller_cert_path = None
    provider._settings.teller_key_path = None
    provider._http()  # must not raise
