"""MCP face — the same core routes, shaped as agent tools.

Run with: ``books-mcp`` (stdio) or ``python -m books.faces.mcp.server``.
Like every face, this is transport only: it holds no rules of its own, so an
agent and a human cannot drift apart on what a category write means.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from books.sdk import BooksAPIError, BooksClient

mcp = MCPServer(
    "books",
    instructions=(
        "Bookkeeping over a business's bank transactions. Categories must come from the "
        "business's chart of accounts — call list_categories before writing one. "
        "Each category has an account_type (asset, liability, equity, revenue, expense); "
        "money out debits the chosen category and money in credits it, so money out is "
        "normally an expense and money in normally revenue — except refunds, transfers, "
        "loan principal and owner draws. "
        "recategorize_transaction and confirm_transaction mark a transaction human-reviewed, "
        "so only call them when a human has actually decided; use categorize_transaction to "
        "let the categorization agent do its own work."
    ),
)


def _call(fn: str, **kwargs: Any) -> Any:
    """Run one core call, turning API errors into readable tool errors."""
    try:
        with BooksClient() as api:
            return getattr(api, fn)(**kwargs)
    except BooksAPIError as exc:
        return {"error": exc.error or "APIError", "status": exc.status_code, "detail": exc.detail}


@mcp.tool()
def list_businesses() -> Any:
    """List the businesses whose books are tracked here."""
    return _call("list_businesses")


@mcp.tool()
def list_categories(business_id: str, account_type: str | None = None) -> Any:
    """Return a business's chart of accounts. Categories must come from this list.

    Each entry carries an account_type (asset, liability, equity, revenue,
    expense) and the normal_balance side that increases it. Optionally filter
    by account_type.
    """
    return _call("list_categories", business_id=business_id, account_type=account_type)


@mcp.tool()
def create_category(
    business_id: str, name: str, account_type: str, description: str | None = None
) -> Any:
    """Add an account to the chart of accounts.

    account_type must be one of: asset, liability, equity, revenue, expense.
    """
    return _call(
        "create_category",
        business_id=business_id,
        name=name,
        account_type=account_type,
        description=description,
    )


@mcp.tool()
def seed_chart_of_accounts(business_id: str) -> Any:
    """Create a standard chart of accounts for a business. Existing names are kept."""
    return _call("seed_chart_of_accounts", business_id=business_id)


@mcp.tool()
def list_transactions(
    business_id: str,
    needs_review: bool | None = None,
    uncategorized: bool | None = None,
    search: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> Any:
    """Search transactions. Dates are YYYY-MM-DD; amounts are negative for money out."""
    return _call(
        "list_transactions",
        business_id=business_id,
        needs_review=needs_review,
        uncategorized=uncategorized,
        search=search,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        offset=offset,
    )


@mcp.tool()
def get_transaction(transaction_id: str) -> Any:
    """Fetch one transaction."""
    return _call("get_transaction", transaction_id=transaction_id)


@mcp.tool()
def transaction_history(transaction_id: str) -> Any:
    """Every category this transaction has had, with actor, confidence and rationale."""
    return _call("transaction_history", transaction_id=transaction_id)


@mcp.tool()
def categorize_transaction(transaction_id: str, wait: bool = True) -> Any:
    """Run the categorization agent over a transaction. Does not mark it reviewed."""
    return _call("categorize", transaction_id=transaction_id, wait=wait)


@mcp.tool()
def recategorize_transaction(
    transaction_id: str, category_name: str, actor: str, note: str | None = None
) -> Any:
    """Set a category on a human's behalf and mark the transaction reviewed.

    ``actor`` must identify the human who decided (e.g. "cli:hakeem"), not the
    agent making the call.
    """
    return _call(
        "recategorize",
        transaction_id=transaction_id,
        category_name=category_name,
        actor=actor,
        note=note,
    )


@mcp.tool()
def confirm_transaction(transaction_id: str, actor: str, note: str | None = None) -> Any:
    """Confirm the current category on a human's behalf and mark it reviewed."""
    return _call("confirm", transaction_id=transaction_id, actor=actor, note=note)


@mcp.tool()
def list_rules(business_id: str) -> Any:
    """Standing rules that pin a vendor to a category, overriding the model."""
    return _call("list_rules", business_id=business_id)


@mcp.tool()
def create_rule(
    business_id: str,
    pattern: str,
    category_name: str,
    match_type: str = "vendor_contains",
    note: str | None = None,
    created_by: str | None = None,
) -> Any:
    """Create a standing rule.

    match_type: vendor_equals | vendor_contains | description_contains.
    """
    return _call(
        "create_rule",
        business_id=business_id,
        match_type=match_type,
        pattern=pattern,
        category_name=category_name,
        note=note,
        created_by=created_by,
    )


@mcp.tool()
def sync_now(business_id: str | None = None, item_id: str | None = None) -> Any:
    """Queue a transaction sync for an item, a business, or everything."""
    return _call("sync", business_id=business_id, item_id=item_id, wait=False)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
