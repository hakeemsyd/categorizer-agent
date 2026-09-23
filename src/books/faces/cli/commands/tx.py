from __future__ import annotations

from datetime import datetime

import typer
from rich.panel import Panel
from rich.table import Table

from books.faces.cli import console as ui
from books.faces.cli.commands import _resolve
from books.sdk import BooksAPIError, BooksClient

app = typer.Typer(no_args_is_help=True)


def _date(value: str | None, flag: str):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError:
        ui.fail(f"{flag} must be YYYY-MM-DD")


@app.command("list")
def list_transactions(
    business: str = typer.Option(None, "--business", "-b"),
    needs_review: bool = typer.Option(False, "--needs-review", help="Only low-confidence rows."),
    uncategorized: bool = typer.Option(False, "--uncategorized"),
    reviewed: bool = typer.Option(False, "--reviewed", help="Only rows a human has confirmed."),
    not_reviewed: bool = typer.Option(
        False, "--not-reviewed", help="Only rows nobody has reviewed yet."
    ),
    search: str = typer.Option(None, "--search", "-s", help="Vendor/description substring."),
    start: str = typer.Option(None, "--from", help="YYYY-MM-DD"),
    end: str = typer.Option(None, "--to", help="YYYY-MM-DD"),
    limit: int = typer.Option(50, "--limit", "-n"),
    offset: int = typer.Option(0, "--offset"),
) -> None:
    """List transactions."""
    if reviewed and not_reviewed:
        ui.fail("--reviewed and --not-reviewed are mutually exclusive")

    try:
        with ui.client() as api:
            business_id = _resolve.business_id(api, business)
            page = api.list_transactions(
                business_id=business_id,
                needs_review=True if needs_review else None,
                uncategorized=True if uncategorized else None,
                reviewed=True if reviewed else (False if not_reviewed else None),
                search=search,
                start_date=_date(start, "--from"),
                end_date=_date(end, "--to"),
                limit=limit,
                offset=offset,
            )
            names = _resolve.category_names(api, business_id)
    except BooksAPIError as exc:
        ui.handle(exc)
        return

    if not page["items"]:
        ui.console.print("[dim]Nothing matches.[/]")
        return
    ui.console.print(ui.transactions_table(page["items"], names))
    shown = page["offset"] + len(page["items"])
    ui.console.print(f"[dim]{shown} of {page['total']}[/]")


@app.command("show")
def show_transaction(transaction_id: str = typer.Argument(..., help="Transaction id.")) -> None:
    """Show one transaction with its full categorization history."""
    try:
        with ui.client() as api:
            transaction = api.get_transaction(transaction_id)
            history = api.transaction_history(transaction_id)
            names = _resolve.category_names(api, transaction["business_id"])
    except BooksAPIError as exc:
        ui.handle(exc)
        return

    side = transaction.get("entry_side")
    body = [
        f"[bold]{transaction.get('vendor') or transaction.get('description') or '—'}[/]",
        f"{transaction['date']}   {ui.money(transaction['amount'])}   "
        f"{ui.entry_side(side)} ({'money out' if side == 'debit' else 'money in'})",
        f"category: {names.get(str(transaction.get('category_id')), '[dim]uncategorized[/]')}",
        f"confidence: {transaction.get('confidence') or '—'}   "
        f"needs review: {'yes' if transaction['needs_review'] else 'no'}",
        f"last reviewed: {transaction.get('last_reviewed_at') or '[dim]never[/]'}",
        f"provider hint: [dim]{transaction.get('provider_category') or '—'}[/]",
        f"description: [dim]{transaction.get('description') or '—'}[/]",
    ]
    ui.console.print(Panel("\n".join(body), title=ui.short(transaction["id"], 8), expand=False))

    if history:
        table = Table(box=None, header_style="bold", title="history", title_justify="left")
        table.add_column("when")
        table.add_column("actor")
        table.add_column("category")
        table.add_column("conf", justify="right")
        table.add_column("rationale")
        for entry in history:
            table.add_row(
                entry["created_at"][:19],
                entry["actor"],
                names.get(str(entry.get("category_id")), "[dim]—[/]"),
                str(entry.get("confidence") or ""),
                (entry.get("rationale") or "")[:70],
            )
        ui.console.print(table)


@app.command("categorize")
def categorize(
    transaction_id: str = typer.Argument(..., help="Transaction id."),
    queue: bool = typer.Option(False, "--queue", help="Hand to a worker instead of waiting."),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite even if a human already reviewed this transaction.",
    ),
) -> None:
    """Run the categorization agent over one transaction.

    Refuses if a human already reviewed it — pass --force to override.
    """
    try:
        with ui.client() as api:
            result = api.categorize(transaction_id, wait=not queue, force=force)
    except BooksAPIError as exc:
        ui.handle(exc)
        return

    if result.get("queued_task_id"):
        ui.ok(f"Queued ({result['queued_task_id']})")
        return
    account_type = result.get("account_type")
    ui.ok(
        f"{result.get('category_name') or 'uncategorized'}"
        # Parentheses, not brackets: Rich would read [expense] as a style tag.
        + (f" [dim]({account_type})[/]" if account_type else "")
        + f" (confidence {result.get('confidence'):.2f}, via {result.get('source')})"
    )
    if result.get("rationale"):
        ui.console.print(f"  [dim]{result['rationale']}[/]")


@app.command("categorize-batch")
def categorize_batch(
    business: str = typer.Option(None, "--business", "-b"),
    uncategorized: bool = typer.Option(False, "--uncategorized", help="Only uncategorized rows."),
    needs_review: bool = typer.Option(
        False, "--needs-review", help="Only rows currently flagged for review."
    ),
    category: str = typer.Option(
        None, "--category", "-c", help="Only rows currently in this category."
    ),
    start: str = typer.Option(None, "--from", help="YYYY-MM-DD"),
    end: str = typer.Option(None, "--to", help="YYYY-MM-DD"),
    include_reviewed: bool = typer.Option(
        False,
        "--include-reviewed",
        help="Also touch transactions a human already reviewed (normally skipped).",
    ),
) -> None:
    """Queue the agent to run again over many transactions at once.

    Useful after editing the chart of accounts, adding a rule, or tuning the
    categorizer — pick a scope with the filters below (default: every
    transaction in the business). Always queued, never inline: this can match
    a lot of rows. Skips anything a human has already reviewed unless you
    pass --include-reviewed.
    """
    try:
        with ui.client() as api:
            business_id = _resolve.business_id(api, business)
            result = api.categorize_batch(
                business_id,
                category_name=category,
                needs_review=True if needs_review else None,
                uncategorized=True if uncategorized else None,
                start_date=_date(start, "--from"),
                end_date=_date(end, "--to"),
                include_reviewed=include_reviewed,
            )
    except BooksAPIError as exc:
        ui.handle(exc)
        return

    ui.ok(f"Queued {result['queued']} transaction(s) for categorization")
    if not include_reviewed:
        ui.console.print("  [dim]Already-reviewed transactions were skipped.[/]")


@app.command("recategorize")
def recategorize(
    transaction_id: str = typer.Argument(..., help="Transaction id."),
    category: str = typer.Option(..., "--category", "-c", help="Category name."),
    note: str = typer.Option("", "--note", help="Why you changed it."),
) -> None:
    """Correct a category. Marks the transaction human-reviewed."""
    try:
        with ui.client() as api:
            api.recategorize(
                transaction_id, actor=ui.actor(), category_name=category, note=note or None
            )
    except BooksAPIError as exc:
        ui.handle(exc)
        return
    ui.ok(f"Set to {category} and marked reviewed")


@app.command("confirm")
def confirm(
    transaction_id: str = typer.Argument(..., help="Transaction id."),
    note: str = typer.Option("", "--note"),
) -> None:
    """Agree with the current category and mark it reviewed."""
    try:
        with ui.client() as api:
            api.confirm(transaction_id, actor=ui.actor(), note=note or None)
    except BooksAPIError as exc:
        ui.handle(exc)
        return
    ui.ok("Confirmed")


@app.command("review")
def review(
    business: str = typer.Option(None, "--business", "-b"),
    limit: int = typer.Option(25, "--limit", "-n", help="How many to walk through."),
) -> None:
    """Walk the review queue one transaction at a time.

    At each prompt: Enter confirms, a category name recategorizes, `s` skips,
    `r` also writes a standing rule for that vendor, `q` quits.
    """
    try:
        with ui.client() as api:
            business_id = _resolve.business_id(api, business)
            page = api.list_transactions(business_id=business_id, needs_review=True, limit=limit)
            names = _resolve.category_names(api, business_id)
            if not page["items"]:
                ui.ok("Review queue is empty.")
                return
            _review_loop(api, business_id, page["items"], names)
    except BooksAPIError as exc:
        ui.handle(exc)


def _review_loop(
    api: BooksClient, business_id: str, rows: list[dict], names: dict[str, str]
) -> None:
    total = len(rows)
    for index, row in enumerate(rows, start=1):
        current = names.get(str(row.get("category_id")), "uncategorized")
        ui.console.print()
        ui.console.print(
            Panel(
                f"[bold]{row.get('vendor') or row.get('description') or '—'}[/]\n"
                f"{row['date']}   {ui.money(row['amount'])}\n"
                f"{ui.entry_side(row.get('entry_side'))} "
                f"agent says: [cyan]{current}[/] "
                f"(confidence {row.get('confidence') or '—'})\n"
                f"[dim]{(row.get('description') or '')[:100]}[/]",
                title=f"{index}/{total}  {ui.short(row['id'], 8)}",
                expand=False,
            )
        )
        answer = ui.console.input(
            "[bold]category[/] (Enter=confirm, name=correct, r=rule, s=skip, q=quit): "
        ).strip()

        if answer.lower() == "q":
            ui.console.print("[dim]Stopped.[/]")
            return
        if answer.lower() == "s":
            continue
        if answer == "":
            if row.get("category_id") is None:
                ui.err_console.print("[yellow]Nothing to confirm — type a category name.[/]")
                continue
            api.confirm(row["id"], actor=ui.actor())
            ui.ok(f"Confirmed as {current}")
            continue

        make_rule = answer.lower() == "r"
        if make_rule:
            answer = ui.console.input("  category for the rule: ").strip()
            if not answer:
                continue

        try:
            api.recategorize(row["id"], actor=ui.actor(), category_name=answer)
        except BooksAPIError as exc:
            ui.err_console.print(f"[yellow]{exc.detail}[/]")
            continue
        ui.ok(f"Set to {answer}")

        if make_rule and row.get("vendor"):
            api.create_rule(
                business_id,
                match_type="vendor_equals",
                pattern=row["vendor"],
                category_name=answer,
                note="Created during review",
                created_by=ui.actor(),
            )
            ui.ok(f"Rule added: vendor {row['vendor']!r} → {answer}")
