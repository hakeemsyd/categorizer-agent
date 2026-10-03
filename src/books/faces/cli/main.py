"""``books`` — the human face.

Every command is a thin call into the core service over HTTP. Nothing here
knows about Fintable, Postgres, or Celery.
"""

from __future__ import annotations

import typer

from books import __version__
from books.faces.cli import console as ui
from books.faces.cli import context
from books.faces.cli.commands import business, category, connections, link, rules, sync, tx
from books.sdk import BooksAPIError

app = typer.Typer(
    name="books",
    help="Bookkeeping automation: sync bank transactions, categorize them, review the odd ones.",
    no_args_is_help=True,
    add_completion=False,
)

app.add_typer(business.app, name="business", help="Tenants and businesses.")
app.add_typer(category.app, name="category", help="Chart of accounts.")
app.add_typer(connections.app, name="connection", help="Linked institutions and accounts.")
app.add_typer(rules.app, name="rule", help="Standing categorization rules.")
app.add_typer(tx.app, name="tx", help="Transactions: list, review, correct.")
app.command("link")(link.link)
app.command("sync")(sync.sync)


@app.command()
def version() -> None:
    """Print the client version."""
    ui.console.print(f"books {__version__}")


@app.command()
def health() -> None:
    """Check that the core service and its database are reachable."""
    try:
        with ui.client() as api:
            data = api.health()
    except BooksAPIError as exc:
        ui.handle(exc)
        return

    status = data["status"]
    colour = "green" if status == "ok" else "yellow"
    ui.console.print(f"[bold {colour}]{status}[/]  v{data['version']}  env={data['env']}")
    ui.console.print(f"database: {data['database']}")
    ui.console.print(f"providers: {', '.join(data['providers'])}")


@app.command()
def init(
    tenant: str = typer.Option(..., "--tenant", help="Name of the account holder."),
    first_business: str | None = typer.Option(
        None, "--business", help="Create the first business at the same time."
    ),
    details: str = typer.Option("", "--details", help="What that business does."),
    seed: bool = typer.Option(
        True, "--seed/--no-seed", help="Give the business a standard chart of accounts."
    ),
) -> None:
    """Create the tenant (and optionally a first business) in an empty database."""
    try:
        with ui.client() as api:
            created = api.create_tenant(tenant)
            ui.ok(f"Tenant {created['name']} ({ui.short(created['id'], 8)})")

            if not first_business:
                ui.console.print("  [dim]Next: books business add NAME[/]")
                return

            created_business = api.create_business(first_business, details or None)
            ui.ok(f"Business {created_business['name']} ({ui.short(created_business['id'], 8)})")

            if seed:
                result = api.seed_chart_of_accounts(created_business["id"])
                ui.ok(f"Chart of accounts: {len(result['created'])} accounts")

            context.set_business(
                api.base_url,
                business_id=str(created_business["id"]),
                name=created_business["name"],
            )
            ui.console.print(
                "  [dim]books category list      review the chart of accounts\n"
                "  books link               connect a bank[/]"
            )
    except BooksAPIError as exc:
        ui.handle(exc)


if __name__ == "__main__":
    app()
