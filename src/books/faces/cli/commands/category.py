from __future__ import annotations

import csv
from pathlib import Path

import typer
from rich.table import Table

from books.core.accounting import AccountType, normal_balance
from books.faces.cli import console as ui
from books.faces.cli.commands import _resolve
from books.sdk import BooksAPIError

app = typer.Typer(no_args_is_help=True)

TYPES = ", ".join(t.value for t in AccountType)

# Colour by account type so a chart of accounts is scannable.
TYPE_STYLE = {
    "revenue": "green",
    "expense": "red",
    "asset": "cyan",
    "liability": "yellow",
    "equity": "magenta",
}


def _validate_type(value: str) -> str:
    try:
        return AccountType(value.strip().lower()).value
    except ValueError:
        ui.fail(f"{value!r} is not an account type. One of: {TYPES}")


def _type_cell(account_type: str) -> str:
    style = TYPE_STYLE.get(account_type, "white")
    return f"[{style}]{account_type}[/]"


@app.command("list")
def list_categories(
    business: str = typer.Option(None, "--business", "-b", help="Business name or id."),
    account_type: str = typer.Option(None, "--type", "-t", help=f"Filter: {TYPES}"),
    include_archived: bool = typer.Option(False, "--all", help="Include archived categories."),
) -> None:
    """Show a business's chart of accounts."""
    if account_type:
        account_type = _validate_type(account_type)
    try:
        with ui.client() as api:
            business_id = _resolve.business_id(api, business)
            rows = api.list_categories(
                business_id, include_archived=include_archived, account_type=account_type
            )
    except BooksAPIError as exc:
        ui.handle(exc)
        return

    if not rows:
        ui.console.print("[dim]No categories yet. Run `books category seed` to start.[/]")
        return

    table = Table(box=None, header_style="bold")
    table.add_column("id", style="dim")
    table.add_column("name")
    table.add_column("type")
    table.add_column("normal", justify="center")
    table.add_column("description")
    for row in rows:
        name = row["name"] + (" [dim](archived)[/]" if row["archived"] else "")
        table.add_row(
            ui.short(row["id"], 8),
            name,
            _type_cell(row["account_type"]),
            row["normal_balance"],
            (row.get("description") or "")[:52],
        )
    ui.console.print(table)

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["account_type"]] = counts.get(row["account_type"], 0) + 1
    summary = "  ".join(f"{_type_cell(t)} {n}" for t, n in sorted(counts.items()))
    ui.console.print(f"\n{len(rows)} accounts:  {summary}")


@app.command("seed")
def seed_categories(
    business: str = typer.Option(None, "--business", "-b"),
) -> None:
    """Create a standard chart of accounts.

    Idempotent — categories you already have are left exactly as they are.
    """
    try:
        with ui.client() as api:
            business_id = _resolve.business_id(api, business)
            result = api.seed_chart_of_accounts(business_id)
    except BooksAPIError as exc:
        ui.handle(exc)
        return

    created, skipped = result["created"], result["skipped"]
    counts: dict[str, int] = {}
    for row in created:
        counts[row["account_type"]] = counts.get(row["account_type"], 0) + 1

    if created:
        summary = "  ".join(f"{_type_cell(t)} {n}" for t, n in sorted(counts.items()))
        ui.ok(f"Added {len(created)} accounts:  {summary}")
    if skipped:
        ui.console.print(
            f"[dim]Kept {len(skipped)} existing: {', '.join(skipped[:6])}"
            + (" …" if len(skipped) > 6 else "")
            + "[/]"
        )
    if not created and not skipped:
        ui.console.print("[dim]Nothing to do.[/]")


@app.command("add")
def add_category(
    name: str = typer.Argument(..., help="Category name, e.g. 'Software & Subscriptions'."),
    account_type: str = typer.Option(..., "--type", "-t", help=f"One of: {TYPES}"),
    business: str = typer.Option(None, "--business", "-b"),
    description: str = typer.Option("", "--description", "-d", help="Guidance for the agent."),
) -> None:
    """Add one account to the chart of accounts."""
    account_type = _validate_type(account_type)
    try:
        with ui.client() as api:
            business_id = _resolve.business_id(api, business)
            created = api.create_category(business_id, name, account_type, description or None)
    except BooksAPIError as exc:
        ui.handle(exc)
        return
    ui.ok(
        f"Added {created['name']} {_type_cell(created['account_type'])} "
        f"— increases on {created['normal_balance']} ({ui.short(created['id'], 8)})"
    )


@app.command("import")
def import_categories(
    path: Path = typer.Argument(
        ..., exists=True, readable=True, help="CSV with name,account_type,description columns."
    ),
    business: str = typer.Option(None, "--business", "-b"),
) -> None:
    """Bulk-load a chart of accounts from CSV.

    Columns: `name`, `account_type` (one of asset, liability, equity, revenue,
    expense) and an optional `description`.
    """
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = [row for row in reader if (row.get("name") or "").strip()]

    if not rows:
        ui.fail(f"{path} has no rows with a 'name' column")

    missing = [r["name"] for r in rows if not (r.get("account_type") or "").strip()]
    if missing:
        ui.fail(
            f"{len(missing)} row(s) have no account_type (e.g. {missing[0]!r}). "
            f"Add an account_type column with one of: {TYPES}"
        )
    for row in rows:
        row["account_type"] = _validate_type(row["account_type"])

    created = 0
    try:
        with ui.client() as api:
            business_id = _resolve.business_id(api, business)
            for row in rows:
                api.create_category(
                    business_id,
                    row["name"].strip(),
                    row["account_type"],
                    (row.get("description") or "").strip() or None,
                )
                created += 1
    except BooksAPIError as exc:
        ui.err_console.print(f"[yellow]Stopped after {created} of {len(rows)} categories.[/]")
        ui.handle(exc)
        return
    ui.ok(f"Imported {created} categories")


@app.command("archive")
def archive_category(category_id: str = typer.Argument(..., help="Category id.")) -> None:
    """Archive a category. History rows keep pointing at it."""
    try:
        with ui.client() as api:
            archived = api.archive_category(category_id)
    except BooksAPIError as exc:
        ui.handle(exc)
        return
    ui.ok(f"Archived {archived['name']}")


@app.command("types")
def show_types() -> None:
    """Explain the five account types and which side increases each."""
    table = Table(box=None, header_style="bold")
    table.add_column("type")
    table.add_column("increases on")
    table.add_column("meaning")
    meanings = {
        AccountType.ASSET: "What the business owns or is owed",
        AccountType.LIABILITY: "What the business owes",
        AccountType.EQUITY: "The owner's stake — contributions and draws",
        AccountType.REVENUE: "Income earned from operations",
        AccountType.EXPENSE: "Costs incurred to operate",
    }
    for account_type in AccountType:
        table.add_row(
            _type_cell(account_type.value),
            normal_balance(account_type).value,
            meanings[account_type],
        )
    ui.console.print(table)
    ui.console.print(
        "\n[dim]Money out debits the category you choose; money in credits it.\n"
        "A debit to an expense is a cost; a credit to an expense is a refund.[/]"
    )
