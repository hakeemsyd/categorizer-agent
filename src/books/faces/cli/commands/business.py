from __future__ import annotations

import typer
from rich.table import Table

from books.faces.cli import console as ui
from books.faces.cli import context
from books.faces.cli.commands import _resolve
from books.sdk import BooksAPIError

app = typer.Typer(no_args_is_help=True)


@app.command("list")
def list_businesses() -> None:
    """List businesses under the tenant. The selected one is marked."""
    try:
        with ui.client() as api:
            rows = api.list_businesses()
            selected = context.get_business(api.base_url)
            counts = {str(row["id"]): len(api.list_categories(row["id"])) for row in rows}
    except BooksAPIError as exc:
        ui.handle(exc)
        return

    if not rows:
        ui.console.print("[dim]No businesses yet. Run `books business add NAME`.[/]")
        return

    override = context.env_business()
    table = Table(box=None, header_style="bold")
    table.add_column("", width=1)
    table.add_column("id", style="dim")
    table.add_column("name")
    table.add_column("accounts", justify="right")
    table.add_column("details")
    for row in rows:
        active = selected is not None and str(row["id"]) == selected.id
        accounts = counts.get(str(row["id"]), 0)
        table.add_row(
            "[green]•[/]" if active else "",
            ui.short(row["id"], 8),
            f"[bold]{row['name']}[/]" if active else row["name"],
            str(accounts) if accounts else "[yellow]0[/]",
            (row.get("details") or "")[:44],
        )
    ui.console.print(table)

    if override:
        ui.console.print(f"\n[dim]BOOKS_BUSINESS={override} overrides the selection.[/]")
    elif selected:
        ui.console.print(f"\n[dim]Selected: {selected.name} — change with `books business use`.[/]")
    elif len(rows) > 1:
        ui.console.print("\n[dim]None selected — run `books business use NAME`.[/]")

    if any(not counts.get(str(row["id"])) for row in rows):
        ui.console.print(
            "[yellow]Some businesses have no chart of accounts[/] — "
            "the agent cannot categorize those. Run `books category seed -b NAME`."
        )


@app.command("add")
def add_business(
    name: str = typer.Argument(..., help="Business name, e.g. 'Coding Crafts'."),
    details: str = typer.Option(
        "", "--details", help="What this business does — the agent reads this."
    ),
    seed: bool = typer.Option(
        True, "--seed/--no-seed", help="Give it a standard chart of accounts."
    ),
    use: bool = typer.Option(False, "--use", help="Also select it for subsequent commands."),
) -> None:
    """Create a business, with a chart of accounts ready to go."""
    try:
        with ui.client() as api:
            created = api.create_business(name, details or None)
            ui.ok(f"Created {created['name']} ({ui.short(created['id'], 8)})")

            if seed:
                result = api.seed_chart_of_accounts(created["id"])
                ui.ok(f"Chart of accounts: {len(result['created'])} accounts")

            existing = api.list_businesses()
            if use or len(existing) == 1:
                context.set_business(
                    api.base_url, business_id=str(created["id"]), name=created["name"]
                )
                ui.console.print(f"  [dim]Selected {created['name']} for future commands.[/]")
            elif len(existing) > 1:
                ui.console.print(
                    f"  [dim]Work on it with `books business use {created['name']}`.[/]"
                )
    except BooksAPIError as exc:
        ui.handle(exc)


@app.command("use")
def use_business(
    name: str = typer.Argument(..., help="Business name or id to work on from now on."),
) -> None:
    """Select the business that commands apply to when you omit --business."""
    try:
        with ui.client() as api:
            chosen = _resolve.business(api, name)
            context.set_business(api.base_url, business_id=str(chosen["id"]), name=chosen["name"])
    except BooksAPIError as exc:
        ui.handle(exc)
        return
    ui.ok(f"Now working on {chosen['name']}")


@app.command("current")
def current_business() -> None:
    """Show which business commands apply to, and why."""
    override = context.env_business()
    try:
        with ui.client() as api:
            chosen = _resolve.business(api, None)
            selected = context.get_business(api.base_url)
            total = len(api.list_businesses())
    except BooksAPIError as exc:
        ui.handle(exc)
        return

    if override:
        reason = f"BOOKS_BUSINESS={override}"
    elif selected:
        reason = "selected with `books business use`"
    else:
        reason = "the only business" if total == 1 else "unknown"

    ui.console.print(f"[bold]{chosen['name']}[/]  [dim]{chosen['id']}[/]")
    ui.console.print(f"[dim]{reason}[/]")


@app.command("unset")
def unset_business() -> None:
    """Forget the selected business."""
    with ui.client() as api:
        cleared = context.clear_business(api.base_url)
    if cleared:
        ui.ok("Selection cleared")
    else:
        ui.console.print("[dim]Nothing was selected.[/]")
