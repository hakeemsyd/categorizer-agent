"""Shared helper: work out which business a command applies to.

Order of precedence, most explicit first:

1. ``--business`` on the command
2. the ``BOOKS_BUSINESS`` environment variable
3. the business saved by ``books business use``
4. the only business, if there is exactly one
5. otherwise: stop and say how to choose
"""

from __future__ import annotations

from books.faces.cli import console as ui
from books.faces.cli import context
from books.sdk import BooksClient


def _match(businesses: list[dict], ref: str) -> dict | None:
    for entry in businesses:
        if str(entry["id"]) == ref or entry["name"].lower() == ref.lower():
            return entry
    return None


def business(api: BooksClient, ref: str | None) -> dict:
    """Resolve to a business record, or exit with a useful message."""
    businesses = api.list_businesses()
    if not businesses:
        ui.fail("No businesses yet. Run `books init --tenant ... --business ...`.")

    explicit = ref or context.env_business()
    if explicit:
        found = _match(businesses, explicit)
        if found is None:
            known = ", ".join(b["name"] for b in businesses)
            ui.fail(f"No business matching {explicit!r}. Known: {known}")
        return found

    selected = context.get_business(api.base_url)
    if selected:
        found = _match(businesses, selected.id)
        if found is not None:
            return found
        # Selected on a previous run but gone since — say so rather than
        # silently acting on a different business.
        ui.fail(
            f"The selected business ({selected.name or selected.id}) no longer exists. "
            "Pick another with `books business use NAME`."
        )

    if len(businesses) == 1:
        return businesses[0]

    known = "\n".join(f"    {b['name']}" for b in businesses)
    ui.fail(
        "Several businesses exist — choose one:\n"
        f"{known}\n\n"
        "  books business use NAME     remember it for future commands\n"
        "  --business NAME             just this once"
    )


def business_id(api: BooksClient, ref: str | None) -> str:
    return str(business(api, ref)["id"])


def category_names(api: BooksClient, business_ref: str) -> dict[str, str]:
    """Category id -> "Name (type)", for rendering transactions."""
    return {
        str(c["id"]): f"{c['name']} [dim]({c['account_type']})[/]"
        for c in api.list_categories(business_ref, include_archived=True)
    }
