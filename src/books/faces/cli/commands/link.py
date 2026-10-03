"""``books link`` — the Fintable OAuth 2.0 handshake, driven from the terminal.

Fintable is a *public* OAuth client: there is no client secret, and the docs
are explicit that none is ever accepted. What stands in for one is PKCE — a
random verifier generated here, sent only as its SHA-256 hash when the browser
is dispatched, and revealed only when the authorization code is redeemed. That
is what stops someone who intercepts the code from using it.

So the shape differs from a widget-based link in one important way: the
verifier must stay in this process across the browser round trip, and travel
with the code to the core. Everything else is familiar — serve a loopback page,
wait for the redirect, hand the result to the core, which does the token swap.
The tokens themselves never pass through the CLI's own logic.

The redirect URI must match what is registered on the Fintable app exactly.
Loopback is explicitly allowed, which is why this can work from a terminal at
all.
"""

from __future__ import annotations

import json
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from queue import Empty, Queue
from urllib.parse import parse_qs, urlparse

import typer

from books.config import get_settings
from books.faces.cli import console as ui
from books.faces.cli.commands import _resolve
from books.providers.fintable import PkceChallenge
from books.sdk import BooksAPIError

CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 8420

_DONE_PAGE = b"""<!doctype html>
<html><head><meta charset="utf-8"><title>Linked</title>
<style>
 body{font:16px/1.5 -apple-system,system-ui,sans-serif;margin:0;display:grid;
      place-connections:center;height:100vh;background:#0f1115;color:#e8eaed}
 .card{max-width:32rem;padding:2rem;text-align:center}
 .muted{color:#9aa0a6}
</style></head>
<body><div class="card">
  <h1>Done</h1>
  <p class="muted">You can close this tab and return to the terminal.</p>
</div></body></html>
"""


def _callback_path() -> str:
    """The path component of the configured redirect URI."""
    return urlparse(get_settings().fintable_redirect_uri).path.rstrip("/")


def _serve(results: Queue) -> HTTPServer:
    """A one-shot loopback server for the OAuth redirect.

    Only the redirect path is handled. Anything else gets a 404 rather than a
    confusing success page, which makes a misconfigured redirect URI obvious
    instead of looking like a hang.
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            # Accept whatever path the configured redirect URI names, so
            # changing the registration does not silently 404 here.
            if parsed.path.rstrip("/") != _callback_path():
                self.send_response(404)
                self.end_headers()
                return
            query = parse_qs(parsed.query)
            results.put({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(_DONE_PAGE)))
            self.end_headers()
            self.wfile.write(_DONE_PAGE)

        def log_message(self, *_: object) -> None:
            """Silence the stdlib access log — the CLI does its own output."""

    server = HTTPServer((CALLBACK_HOST, CALLBACK_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _why(payload: dict) -> str:
    """Turn an OAuth error redirect into something worth reading."""
    error = payload.get("error", "")
    description = payload.get("error_description", "")
    if error == "access_denied":
        return "Declined in the browser."
    hints = {
        "invalid_request": (
            "Often the redirect URI: it must match what is registered on the "
            "Fintable app character for character, including the port."
        ),
        "invalid_client": "BOOKS_FINTABLE_CLIENT_ID does not match a Fintable app.",
        "invalid_grant": "The code was already used or expired. Try again.",
    }
    parts = [p for p in (error, description, hints.get(error, "")) if p]
    return "Fintable refused the authorization: " + " / ".join(parts)


def link(
    business: str = typer.Option(
        None, "--business", "-b", help="Business to attach the accounts to."
    ),
    provider: str = typer.Option(None, "--provider", help="Defaults to BOOKS_DEFAULT_PROVIDER."),
    since: str = typer.Option(
        None, "--since", help="Backfill boundary (YYYY-MM-DD), stored on the connection."
    ),
    timeout: int = typer.Option(300, "--timeout", help="Seconds to wait for the browser flow."),
) -> None:
    """Connect Fintable via OAuth 2.0."""
    backfill_start = None
    if since:
        try:
            backfill_start = datetime.strptime(since, "%Y-%m-%d").date()
        except ValueError:
            ui.fail("--since must be YYYY-MM-DD")

    # Minted here and kept here. The verifier is the secret that makes a
    # public client safe, so it must not go anywhere until the code is
    # redeemed — at which point it travels with the code, together, once.
    pkce = PkceChallenge.generate()

    results: Queue = Queue()
    server = None
    try:
        with ui.client() as api:
            business_id = _resolve.business_id(api, business)
            token = api.create_link_token(business_id, provider)
            if not token.get("authorize_url"):
                ui.fail(
                    f"Provider {token.get('provider')!r} did not return an authorization URL. "
                    "OAuth linking needs one; check BOOKS_DEFAULT_PROVIDER."
                )
                return

            from urllib.parse import urlencode

            url = (
                token["authorize_url"]
                + "?"
                + urlencode(
                    {
                        "client_id": token["link_token"],
                        "redirect_uri": token["redirect_uri"],
                        "response_type": "code",
                        "scope": token["scopes"],
                        "state": pkce.state,
                        "code_challenge": pkce.challenge,
                        "code_challenge_method": "S256",
                    }
                )
            )

            server = _serve(results)
            ui.console.print("Opening Fintable in your browser to authorize…")
            ui.console.print(f"[dim]If nothing opens, visit:[/]\n{url}\n")
            webbrowser.open(url)

            try:
                payload = results.get(timeout=timeout)
            except Empty:
                ui.fail(
                    f"Timed out after {timeout}s. If the browser showed a redirect error, "
                    f"check that {token['redirect_uri']} is registered on the Fintable app."
                )
                return

            if payload.get("error") or not payload.get("code"):
                ui.fail(_why(payload))
                return

            # Verifying state is the whole reason it was sent. Skipping it
            # would let someone else's authorization code be swapped in.
            if payload.get("state") != pkce.state:
                ui.fail(
                    "The 'state' returned by Fintable did not match the one sent. "
                    "Discarding this response rather than redeeming it."
                )
                return

            connection = api.exchange_public_token(
                business_id,
                json.dumps({"code": payload["code"], "code_verifier": pkce.verifier}),
                provider,
                backfill_start_date=backfill_start,
            )
    except BooksAPIError as exc:
        ui.handle(exc)
        return
    finally:
        if server is not None:
            server.shutdown()

    name = connection.get("institution_name") or "Fintable"
    ui.ok(f"Linked {name} (connection {ui.short(connection['id'], 8)})")
    hint = f" --since {since}" if since else ""
    ui.console.print(
        f"  Next: [bold]books sync --connection {connection['id']} --backfill{hint}[/]"
    )
