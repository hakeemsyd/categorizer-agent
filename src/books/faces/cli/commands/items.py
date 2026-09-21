from __future__ import annotations

import typer
from rich.table import Table

from books.faces.cli import console as ui
from books.faces.cli.commands import _resolve
from books.sdk import BooksAPIError

app = typer.Typer(no_args_is_help=True)


@app.command("list")
def list_items(
    business: str = typer.Option(None, "--business", "-b"),
    all_businesses: bool = typer.Option(False, "--all", help="Every business."),
) -> None:
    """Linked institutions and their sync status."""
    try:
        with ui.client() as api:
            business_id = None if all_businesses else _resolve.business_id(api, business)
            rows = api.list_items(business_id)
    except BooksAPIError as exc:
        ui.handle(exc)
        return

    if not rows:
        ui.console.print("[dim]No institutions linked yet. Run `books link`.[/]")
        return

    table = Table(box=None, header_style="bold")
    table.add_column("id", style="dim")
    table.add_column("institution")
    table.add_column("provider")
    table.add_column("status")
    table.add_column("last synced")
    table.add_column("backfill from")
    for row in rows:
        status = row["status"]
        colour = {"active": "green", "error": "red", "disconnected": "yellow"}.get(status, "white")
        table.add_row(
            ui.short(row["id"], 8),
            row.get("institution_name") or "—",
            row["provider"],
            f"[{colour}]{status}[/]",
            (row.get("last_synced_at") or "never")[:19],
            str(row.get("backfill_start_date") or "—"),
        )
    ui.console.print(table)
    for row in rows:
        if row.get("last_error"):
            ui.err_console.print(f"[red]{ui.short(row['id'], 8)}: {row['last_error'][:200]}[/]")


@app.command("accounts")
def list_accounts(
    business: str = typer.Option(None, "--business", "-b"),
    all_businesses: bool = typer.Option(False, "--all", help="Every business."),
) -> None:
    """Bank accounts discovered under the linked institutions."""
    try:
        with ui.client() as api:
            business_id = None if all_businesses else _resolve.business_id(api, business)
            rows = api.list_accounts(business_id)
    except BooksAPIError as exc:
        ui.handle(exc)
        return

    table = Table(box=None, header_style="bold")
    table.add_column("id", style="dim")
    table.add_column("name")
    table.add_column("type")
    table.add_column("class")
    table.add_column("balance", justify="right")
    for row in rows:
        table.add_row(
            ui.short(row["id"], 8),
            row["name"],
            row.get("account_type") or "",
            row.get("classification") or "",
            ui.money(row.get("current_balance")),
        )
    ui.console.print(table)


@app.command("refresh")
def refresh(item_id: str = typer.Argument(..., help="Item id.")) -> None:
    """Ask the provider for fresh data now (the webhook does the rest)."""
    try:
        with ui.client() as api:
            api.refresh_accounts(item_id)
            api.force_refresh(item_id)
    except BooksAPIError as exc:
        ui.handle(exc)
        return
    ui.ok("Refresh requested")
