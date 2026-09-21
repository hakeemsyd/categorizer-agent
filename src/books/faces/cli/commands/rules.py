from __future__ import annotations

import typer
from rich.table import Table

from books.faces.cli import console as ui
from books.faces.cli.commands import _resolve
from books.sdk import BooksAPIError

app = typer.Typer(no_args_is_help=True)

MATCH_TYPES = ("vendor_equals", "vendor_contains", "description_contains")


@app.command("list")
def list_rules(
    business: str = typer.Option(None, "--business", "-b"),
    include_inactive: bool = typer.Option(False, "--all"),
) -> None:
    """Standing rules the categorizer must honour."""
    try:
        with ui.client() as api:
            business_id = _resolve.business_id(api, business)
            rows = api.list_rules(business_id, active_only=not include_inactive)
            names = _resolve.category_names(api, business_id)
    except BooksAPIError as exc:
        ui.handle(exc)
        return

    if not rows:
        ui.console.print("[dim]No rules yet.[/]")
        return

    table = Table(box=None, header_style="bold")
    table.add_column("id", style="dim")
    table.add_column("match")
    table.add_column("pattern")
    table.add_column("category")
    table.add_column("prio", justify="right")
    table.add_column("active")
    for row in rows:
        table.add_row(
            ui.short(row["id"], 8),
            row["match_type"],
            row["pattern"],
            names.get(str(row["category_id"]), ""),
            str(row["priority"]),
            "yes" if row["active"] else "[dim]no[/]",
        )
    ui.console.print(table)


@app.command("add")
def add_rule(
    pattern: str = typer.Argument(..., help="Text to match, e.g. 'AWS'."),
    category: str = typer.Option(..., "--category", "-c", help="Category name to assign."),
    business: str = typer.Option(None, "--business", "-b"),
    match_type: str = typer.Option(
        "vendor_contains", "--match", "-m", help=f"One of: {', '.join(MATCH_TYPES)}"
    ),
    note: str = typer.Option("", "--note", help="Why this rule exists."),
    priority: int = typer.Option(100, "--priority", help="Lower wins."),
) -> None:
    """Pin a vendor to a category. Rules beat the model, every time."""
    if match_type not in MATCH_TYPES:
        ui.fail(f"--match must be one of: {', '.join(MATCH_TYPES)}")

    try:
        with ui.client() as api:
            business_id = _resolve.business_id(api, business)
            created = api.create_rule(
                business_id,
                match_type=match_type,
                pattern=pattern,
                category_name=category,
                note=note or None,
                priority=priority,
                created_by=ui.actor(),
            )
    except BooksAPIError as exc:
        ui.handle(exc)
        return
    ui.ok(f"Rule {ui.short(created['id'], 8)}: {match_type}={pattern!r} → {category}")


@app.command("rm")
def remove_rule(rule_id: str = typer.Argument(..., help="Rule id.")) -> None:
    """Deactivate a rule."""
    try:
        with ui.client() as api:
            api.deactivate_rule(rule_id)
    except BooksAPIError as exc:
        ui.handle(exc)
        return
    ui.ok("Rule deactivated")
