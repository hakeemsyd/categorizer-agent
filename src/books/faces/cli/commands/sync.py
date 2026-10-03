from __future__ import annotations

from datetime import datetime

import typer

from books.faces.cli import console as ui
from books.faces.cli.commands import _resolve
from books.sdk import BooksAPIError


def sync(
    business: str = typer.Option(None, "--business", "-b", help="Business name or id."),
    connection: str = typer.Option(None, "--connection", help="Sync just this connection."),
    backfill: bool = typer.Option(
        False, "--backfill", help="Restart from the beginning of the provider's history."
    ),
    since: str = typer.Option(
        None, "--since", help="Discard anything dated before this (YYYY-MM-DD). Backfill only."
    ),
    all_businesses: bool = typer.Option(
        False, "--all", help="Sync every business, ignoring the selected one."
    ),
    wait: bool = typer.Option(
        True, "--wait/--queue", help="Run now and show the result, or hand it to a worker."
    ),
) -> None:
    """Pull new transactions from the provider.

    A backfill still walks the provider's full cursor feed — no aggregator
    exposes a date range on its sync endpoint — but `--since` drops anything
    older before it reaches the database.
    """
    since_date = None
    if since:
        try:
            since_date = datetime.strptime(since, "%Y-%m-%d").date()
        except ValueError:
            ui.fail("--since must be YYYY-MM-DD")

    if since_date and not backfill:
        ui.console.print(
            "[yellow]note[/] --since only bounds a --backfill; ongoing syncs always take "
            "everything the provider reports."
        )

    try:
        with ui.client() as api:
            # --all and --connection both mean "do not scope this to one business".
            business_id = (
                None if (all_businesses or connection) else _resolve.business_id(api, business)
            )
            response = api.sync(
                connection_id=connection,
                business_id=business_id,
                backfill=backfill,
                since=since_date,
                wait=wait,
            )
    except BooksAPIError as exc:
        ui.handle(exc)
        return

    for result in response["results"]:
        label = ui.short(result["connection_id"], 8)
        if result.get("queued_task_id"):
            ui.ok(f"{label}: queued ({result['queued_task_id']})")
            continue
        ui.ok(
            f"{label}: {result['inserted']} new, {result['updated']} updated, "
            f"{result['removed']} removed"
            + (
                f", {result['skipped_before_backfill']} older than --since"
                if result["skipped_before_backfill"]
                else ""
            )
        )
        if result["new_transaction_ids"]:
            ui.console.print(
                f"  [dim]{len(result['new_transaction_ids'])} queued for categorization[/]"
            )
