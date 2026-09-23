"""``books link`` — the Teller Connect handshake, driven from the terminal.

The CLI asks the core for a link token (Teller's public `application_id`),
serves Teller Connect locally, and posts the resulting access token straight
back to the core for exchange. Unlike Plaid, Connect hands the browser the
real, finished access token directly in `onSuccess` — there is no separate
public-token exchange step — but the shape of driving it from the terminal is
otherwise identical: serve a local page, wait for its callback, hand the core
whatever came back. The access token still never touches the CLI's own logic;
it passes straight through to the core over one HTTP call.
"""

from __future__ import annotations

import json
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from queue import Empty, Queue

import typer

from books.faces.cli import console as ui
from books.faces.cli.commands import _resolve
from books.sdk import BooksAPIError

CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 8420

# transactions + balance is what this app needs; verify/identity aren't used.
_PRODUCTS = ["transactions", "balance"]

_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Link your bank</title>
<script src="https://cdn.teller.io/connect/connect.js"></script>
<style>
 body{font:16px/1.5 -apple-system,system-ui,sans-serif;margin:0;display:grid;
      place-items:center;height:100vh;background:#0f1115;color:#e8eaed}
 .card{max-width:32rem;padding:2rem;text-align:center}
 .muted{color:#9aa0a6}
</style></head>
<body><div class="card">
  <h1>Linking your institution…</h1>
  <p class="muted" id="status">Opening Teller Connect.</p>
</div>
<script>
  const connect = TellerConnect.setup({
    applicationId: "__APPLICATION_ID__",
    environment: "__ENVIRONMENT__",
    products: __PRODUCTS__,
    onSuccess: async (enrollment) => {
      document.getElementById("status").textContent = "Finishing up — you can close this tab.";
      await fetch("/callback", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({public_token: enrollment.accessToken})
      });
    },
    onExit: async () => {
      document.getElementById("status").textContent = "Cancelled. You can close this tab.";
      await fetch("/callback", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({error: "cancelled"})
      });
    }
  });
  connect.open();
</script></body></html>
"""


def _serve(application_id: str, environment: str, results: Queue) -> HTTPServer:
    page = (
        _PAGE.replace("__APPLICATION_ID__", application_id)
        .replace("__ENVIRONMENT__", environment or "sandbox")
        .replace("__PRODUCTS__", json.dumps(_PRODUCTS))
        .encode()
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                payload = {"error": "malformed callback"}
            results.put(payload)
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_: object) -> None:
            """Silence the stdlib access log — the CLI does its own output."""

    server = HTTPServer((CALLBACK_HOST, CALLBACK_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def link(
    business: str = typer.Option(
        None, "--business", "-b", help="Business to attach the accounts to."
    ),
    provider: str = typer.Option(None, "--provider", help="Defaults to BOOKS_DEFAULT_PROVIDER."),
    since: str = typer.Option(
        None, "--since", help="Backfill boundary (YYYY-MM-DD), stored on the item."
    ),
    timeout: int = typer.Option(300, "--timeout", help="Seconds to wait for the browser flow."),
) -> None:
    """Connect a bank via Teller Connect."""
    backfill_start = None
    if since:
        try:
            backfill_start = datetime.strptime(since, "%Y-%m-%d").date()
        except ValueError:
            ui.fail("--since must be YYYY-MM-DD")

    results: Queue = Queue()
    server = None
    try:
        with ui.client() as api:
            business_id = _resolve.business_id(api, business)
            token = api.create_link_token(business_id, provider)

            server = _serve(token["link_token"], token.get("environment", ""), results)
            url = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}/"
            ui.console.print(f"Opening [bold]{url}[/] — complete the flow in your browser.")
            webbrowser.open(url)

            try:
                payload = results.get(timeout=timeout)
            except Empty:
                ui.fail(f"Timed out after {timeout}s waiting for the browser flow.")
                return

            if payload.get("error") or not payload.get("public_token"):
                ui.fail(f"Link did not complete: {payload.get('error', 'no access token')}")
                return

            item = api.exchange_public_token(
                business_id,
                payload["public_token"],
                provider,
                backfill_start_date=backfill_start,
            )
    except BooksAPIError as exc:
        ui.handle(exc)
        return
    finally:
        if server is not None:
            server.shutdown()

    ui.ok(
        f"Linked {item.get('institution_name') or 'institution'} (item {ui.short(item['id'], 8)})"
    )
    hint = f" --since {since}" if since else ""
    ui.console.print(f"  Next: [bold]books sync --item {item['id']} --backfill{hint}[/]")
