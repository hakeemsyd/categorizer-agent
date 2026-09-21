"""Shared Rich rendering + the CLI's view of the core client."""

from __future__ import annotations

import getpass
import os
from decimal import Decimal
from typing import Any, NoReturn

import typer
from rich.console import Console
from rich.table import Table

from books.sdk import BooksAPIError, BooksClient

console = Console()
err_console = Console(stderr=True)


def client() -> BooksClient:
    return BooksClient()


def actor() -> str:
    """Who a human write is attributed to in categorization_history."""
    return os.environ.get("BOOKS_ACTOR") or f"cli:{getpass.getuser()}"


def fail(message: str) -> NoReturn:
    err_console.print(f"[bold red]✗[/] {message}")
    raise typer.Exit(code=1)


def ok(message: str) -> None:
    console.print(f"[bold green]✓[/] {message}")


def handle(exc: BooksAPIError) -> None:
    fail(f"{exc.error or 'Error'} ({exc.status_code}): {exc.detail}")


def money(value: Any) -> str:
    if value is None:
        return ""
    amount = Decimal(str(value))
    colour = "red" if amount < 0 else "green"
    return f"[{colour}]{amount:,.2f}[/]"


def short(value: Any, width: int = 8) -> str:
    return str(value)[:width] if value else ""


def entry_side(value: str | None) -> str:
    """Dr / Cr, the way a ledger prints it."""
    if value == "debit":
        return "[cyan]Dr[/]"
    if value == "credit":
        return "[magenta]Cr[/]"
    return ""


def transactions_table(rows: list[dict], categories: dict[str, str] | None = None) -> Table:
    categories = categories or {}
    table = Table(box=None, pad_edge=False, header_style="bold")
    table.add_column("id", style="dim")
    table.add_column("date")
    table.add_column("amount", justify="right")
    table.add_column("dr/cr", justify="center")
    table.add_column("vendor")
    table.add_column("category")
    table.add_column("conf", justify="right")
    table.add_column("review")

    for row in rows:
        confidence = row.get("confidence")
        table.add_row(
            short(row["id"]),
            str(row["date"]),
            money(row["amount"]),
            entry_side(row.get("entry_side")),
            (row.get("vendor") or row.get("description") or "")[:34],
            categories.get(str(row.get("category_id")), "") or "[dim]—[/]",
            f"{float(confidence):.2f}" if confidence is not None else "",
            "[yellow]needs review[/]" if row.get("needs_review") else "",
        )
    return table
