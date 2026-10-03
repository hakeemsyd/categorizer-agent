"""The Fintable adapter, against a mocked transport.

Weighted toward the things that are expensive to get wrong rather than the
things that are easy to test: PKCE (which is what stands in for a client
secret), the sign convention on credit cards, and the sync watermark, where an
off-by-one silently loses transactions nobody notices are missing.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from books.config import get_settings
from books.core.accounting import AccountType
from books.core.errors import ProviderError, WebhookVerificationError
from books.providers.fintable import FintableProvider, PkceChallenge, _Cursor


@pytest.fixture(autouse=True)
def _configure(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOOKS_FINTABLE_CLIENT_ID", "cid-123")
    monkeypatch.setenv("BOOKS_FINTABLE_WORKSPACE_ID", "ws-1")
    monkeypatch.setenv("BOOKS_FINTABLE_REDIRECT_URI", "http://127.0.0.1:8420/oauth/callback")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def provider_with(handler) -> FintableProvider:
    return FintableProvider(client=httpx.Client(transport=httpx.MockTransport(handler)))


# --- PKCE: what replaces a client secret ---------------------------------


def test_the_challenge_is_the_sha256_of_the_verifier() -> None:
    """If this is wrong the server rejects every exchange, with no clue why."""
    pkce = PkceChallenge.generate()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(pkce.verifier.encode("ascii")).digest())
        .decode()
        .rstrip("=")
    )
    assert pkce.challenge == expected


def test_each_attempt_gets_its_own_verifier_and_state() -> None:
    a, b = PkceChallenge.generate(), PkceChallenge.generate()
    assert a.verifier != b.verifier
    assert a.state != b.state


def test_the_verifier_is_never_put_in_the_authorization_url() -> None:
    """Sending it would defeat PKCE entirely — only the hash may travel."""
    pkce = PkceChallenge.generate()
    url = provider_with(lambda r: httpx.Response(200)).authorization_url(pkce)
    assert pkce.verifier not in url
    assert pkce.challenge in url
    assert "code_challenge_method=S256" in url


def test_the_exchange_sends_the_verifier_and_never_a_secret(monkeypatch) -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            seen["body"] = request.content.decode()
            return httpx.Response(
                200,
                json={
                    "access_token": "at-1",
                    "refresh_token": "rt-1",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        if request.url.path == "/api/v2/connections":
            return httpx.Response(200, json={"data": [{"name": "Chase", "id": "con-1"}]})
        return httpx.Response(200, json={})

    creds = provider_with(handler).exchange_public_token(
        json.dumps({"code": "the-code", "code_verifier": "the-verifier"})
    )
    assert "code_verifier=the-verifier" in seen["body"]
    assert "client_secret" not in seen["body"]  # Fintable never accepts one
    assert creds.access_token == "at-1"
    assert creds.refresh_token == "rt-1"
    assert creds.expires_at is not None


def test_an_exchange_without_a_verifier_is_refused_before_the_network() -> None:
    with pytest.raises(ProviderError, match="code_verifier"):
        provider_with(lambda r: httpx.Response(500)).exchange_public_token("just-a-code")


# --- rotation ------------------------------------------------------------


def test_refresh_returns_the_new_refresh_token() -> None:
    """Fintable rotates it. Dropping the new one costs the connection."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert "grant_type=refresh_token" in request.content.decode()
        return httpx.Response(
            200, json={"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 3600}
        )

    tokens = provider_with(handler).refresh_tokens("rt-1")
    assert (tokens.access_token, tokens.refresh_token) == ("at-2", "rt-2")
    assert not tokens.expires_soon


def test_a_token_response_with_no_access_token_is_an_error() -> None:
    handler = lambda r: httpx.Response(200, json={"refresh_token": "rt-2"})  # noqa: E731
    with pytest.raises(ProviderError, match="no access_token"):
        provider_with(handler).refresh_tokens("rt-1")


# --- accounts: the sign convention depends on this -----------------------


@pytest.mark.parametrize(
    ("type_text", "expected"),
    [
        ("depository / checking", AccountType.ASSET),
        ("depository / savings", AccountType.ASSET),
        ("credit / credit_card", AccountType.LIABILITY),
        ("loan / mortgage", AccountType.LIABILITY),
        ("investment / brokerage", AccountType.ASSET),
    ],
)
def test_account_family_decides_asset_or_liability(type_text, expected) -> None:
    """This is what makes a card purchase money out rather than income."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"id": "acc-1", "name": "A", "type": type_text, "balance": "10.00"}]},
        )

    assert provider_with(handler).get_accounts("at")[0].classification is expected


def test_an_unrecognized_account_type_does_not_guess() -> None:
    """Guessing liability would invert every amount on the account."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "a", "name": "A", "type": "wat / huh"}]})

    assert provider_with(handler).get_accounts("at")[0].classification is AccountType.ASSET


# --- transactions --------------------------------------------------------


def _txn(**over):
    row = {
        "id": "tx-1",
        "account_id": "acc-1",
        "amount": "-12.34",
        "date": "2026-02-14",
        "description": "BLUE BOTTLE COFFEE",
        "merchant": "Blue Bottle",
        "category": {"id": "c1", "name": "Coffee"},
        "updated_at": "2026-02-14T10:00:00Z",
    }
    row.update(over)
    return row


def test_a_transaction_is_mapped_with_its_sign_intact() -> None:
    handler = lambda r: httpx.Response(200, json={"data": [_txn()], "next_cursor": None})  # noqa: E731
    result = provider_with(handler).sync_transactions("at", None)
    txn = result.added[0]
    assert txn.amount == Decimal("-12.34")  # negative is money out, per Fintable
    assert txn.merchant_name == "Blue Bottle"
    assert txn.provider_category == "Coffee"
    assert txn.raw_payload["id"] == "tx-1"


def test_one_bad_row_does_not_lose_the_good_ones() -> None:
    """A page is expensive to refetch; a single unparseable row is not fatal."""
    rows = [_txn(), _txn(id="tx-2", amount="not-a-number"), _txn(id="tx-3")]
    handler = lambda r: httpx.Response(200, json={"data": rows, "next_cursor": None})  # noqa: E731
    result = provider_with(handler).sync_transactions("at", None)
    assert [t.provider_transaction_id for t in result.added] == ["tx-1", "tx-3"]


def test_a_first_sync_reads_everything_and_sets_a_watermark() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"data": [_txn()], "next_cursor": None})

    result = provider_with(handler).sync_transactions("at", None)
    assert "updated_since" not in seen["params"]  # nothing to be incremental from
    assert result.added and not result.modified
    assert _Cursor.decode(result.next_cursor).updated_since is not None


def test_a_later_sync_only_asks_for_what_changed() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"data": [_txn()], "next_cursor": None})

    cursor = _Cursor(updated_since="2026-02-01T00:00:00+00:00").encode()
    result = provider_with(handler).sync_transactions("at", cursor)
    assert seen["params"]["updated_since"] == "2026-02-01T00:00:00+00:00"
    assert seen["params"]["order"] == "updated"
    # Already-seen rows that moved are modifications, not new transactions.
    assert result.modified and not result.added


def test_the_watermark_is_when_the_walk_started_not_when_it_ended() -> None:
    """Otherwise anything written mid-walk falls into the gap and is never seen."""
    before = datetime.now(tz=UTC)
    handler = lambda r: httpx.Response(200, json={"data": [], "next_cursor": None})  # noqa: E731
    result = provider_with(handler).sync_transactions("at", None)
    after = datetime.now(tz=UTC)
    mark = datetime.fromisoformat(_Cursor.decode(result.next_cursor).updated_since)
    assert before <= mark <= after


def test_every_page_names_the_same_workspace() -> None:
    """Cursors are workspace-bound; replaying one elsewhere is invalid_cursor."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        nxt = None if len(calls) >= 2 else "cur-2"
        return httpx.Response(200, json={"data": [_txn(id=f"tx-{len(calls)}")], "next_cursor": nxt})

    provider_with(handler).sync_transactions("at", None)
    assert len(calls) == 2
    assert {c["workspace_id"] for c in calls} == {"ws-1"}
    assert calls[1]["cursor"] == "cur-2"


def test_a_cursor_from_an_older_format_is_survivable() -> None:
    """A full re-read is slower but correct; failing the sync is neither."""
    assert _Cursor.decode("some:legacy:string").updated_since is None


def test_an_iso_timestamp_survives_a_cursor_round_trip() -> None:
    """The previous provider's colon-joined cursor broke on exactly this."""
    stamp = "2026-02-14T10:00:00+00:00"
    assert _Cursor.decode(_Cursor(updated_since=stamp).encode()).updated_since == stamp


# --- errors and webhooks -------------------------------------------------


def test_a_401_says_the_token_may_have_expired() -> None:
    handler = lambda r: httpx.Response(401, json={"error": "invalid_token"})  # noqa: E731
    with pytest.raises(ProviderError, match="expired or been revoked"):
        provider_with(handler).get_accounts("at")


def test_a_429_explains_that_limits_follow_the_credential() -> None:
    handler = lambda r: httpx.Response(429, json={"error": "slow down"})  # noqa: E731
    with pytest.raises(ProviderError, match="per credential"):
        provider_with(handler).get_accounts("at")


def test_webhooks_are_refused_rather_than_waved_through() -> None:
    """Fintable documents none. Accepting unsigned callbacks would be an opening."""
    with pytest.raises(WebhookVerificationError):
        provider_with(lambda r: httpx.Response(200)).verify_webhook({}, b"{}")


# --- the redirect URI, which is where linking silently breaks -------------


def test_the_authorization_url_sends_the_configured_redirect(monkeypatch) -> None:
    monkeypatch.setenv("BOOKS_FINTABLE_REDIRECT_URI", "http://127.0.0.1:8420/oauth/callback")
    get_settings.cache_clear()
    url = provider_with(lambda r: httpx.Response(200)).authorization_url(PkceChallenge.generate())
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A8420%2Foauth%2Fcallback" in url


def test_the_exchange_repeats_the_same_redirect_uri(monkeypatch) -> None:
    """OAuth requires the token call to echo the redirect sent at authorize.

    Reading both from one setting is what guarantees that; this pins it, since
    a mismatch fails as invalid_grant with nothing pointing at the cause.
    """
    monkeypatch.setenv("BOOKS_FINTABLE_REDIRECT_URI", "http://127.0.0.1:9999/oauth/callback")
    get_settings.cache_clear()
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            seen["body"] = request.content.decode()
            return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt"})
        return httpx.Response(200, json={"data": []})

    provider_with(handler).exchange_public_token(json.dumps({"code": "c", "code_verifier": "v"}))
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A9999%2Foauth%2Fcallback" in seen["body"]
