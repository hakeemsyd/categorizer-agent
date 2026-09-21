#!/usr/bin/env python
"""Populate a database with a demo tenant, business, chart of accounts and
transactions from the FakeProvider — no Plaid credentials, no network.

    BOOKS_DEFAULT_PROVIDER=fake python scripts/seed_demo.py

Useful for trying the CLI, the MCP tools, or the review loop before Plaid
access arrives.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date

from books.core import repository as repo
from books.core.chart_of_accounts import chart_summary
from books.core.db import dispose_engine, session_scope
from books.core.sync import link_item, sync_item
from books.providers.fake import default_state, reset_fake_state


async def main() -> int:
    reset_fake_state(default_state(date.today()))

    async with session_scope() as session:
        if await repo.list_tenants(session):
            print("Database already has a tenant — refusing to seed over it.", file=sys.stderr)
            return 1

        tenant = await repo.create_tenant(session, name="Demo Tenant")
        business = await repo.create_business(
            session,
            tenant_id=tenant.id,
            name="Planning Crafts",
            details=(
                "Software consultancy. Revenue from client retainers; costs are payroll and cloud."
            ),
        )
        categories, _ = await repo.seed_chart_of_accounts(session, business=business)

        item = await link_item(
            session, business=business, provider_name="fake", public_token="demo"
        )
        summary = await sync_item(session, item=item)

    print(f"Tenant:      {tenant.name} ({tenant.id})")
    print(f"Business:    {business.name} ({business.id})")
    print(f"Categories:  {len(categories)} ({chart_summary()})")
    print(f"Institution: {item.institution_name} (item {item.id})")
    print(f"Transactions:{summary.inserted} inserted")
    print()
    print("Try:  books tx list --business 'Coding Crafts'")
    await dispose_engine()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
