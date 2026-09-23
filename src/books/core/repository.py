"""Data access. Plain functions over an ``AsyncSession`` — no HTTP, no Celery.

Everything the faces need to read or write lives here or in the sibling
service modules (``categorization``, ``ingest``, ``sync``).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, cast

from sqlalchemy import CursorResult, Select, delete, func, literal_column, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from books.core.accounting import AccountType
from books.core.chart_of_accounts import DEFAULT_CHART
from books.core.errors import NotFoundError, ValidationError
from books.core.models import (
    Account,
    Business,
    CategorizationHistory,
    Category,
    Item,
    Rule,
    Tenant,
    Transaction,
)
from books.providers.base import RawAccount

# --- tenants ------------------------------------------------------------


async def create_tenant(session: AsyncSession, *, name: str) -> Tenant:
    tenant = Tenant(name=name)
    session.add(tenant)
    await session.flush()
    return tenant


async def list_tenants(session: AsyncSession) -> Sequence[Tenant]:
    return (await session.scalars(select(Tenant).order_by(Tenant.created_at))).all()


async def get_tenant(session: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise NotFoundError(f"Tenant {tenant_id} not found")
    return tenant


async def default_tenant(session: AsyncSession) -> Tenant:
    """v1 is single-tenant: the first tenant is *the* tenant."""
    tenant = await session.scalar(select(Tenant).order_by(Tenant.created_at).limit(1))
    if tenant is None:
        raise NotFoundError("No tenant exists yet. Run `books init` or POST /tenants.")
    return tenant


# --- businesses ---------------------------------------------------------


async def create_business(
    session: AsyncSession, *, tenant_id: uuid.UUID, name: str, details: str | None = None
) -> Business:
    business = Business(tenant_id=tenant_id, name=name, details=details)
    session.add(business)
    await session.flush()
    return business


async def list_businesses(
    session: AsyncSession, *, tenant_id: uuid.UUID | None = None
) -> Sequence[Business]:
    stmt = select(Business).order_by(Business.name)
    if tenant_id:
        stmt = stmt.where(Business.tenant_id == tenant_id)
    return (await session.scalars(stmt)).all()


async def get_business(session: AsyncSession, business_id: uuid.UUID) -> Business:
    business = await session.get(Business, business_id)
    if business is None:
        raise NotFoundError(f"Business {business_id} not found")
    return business


async def resolve_business(session: AsyncSession, ref: str) -> Business:
    """Accept either a UUID or a business name — the CLI passes whichever."""
    try:
        return await get_business(session, uuid.UUID(ref))
    except ValueError:
        pass
    business = await session.scalar(
        select(Business).where(func.lower(Business.name) == ref.lower())
    )
    if business is None:
        raise NotFoundError(f"No business named {ref!r}")
    return business


# --- categories ---------------------------------------------------------


async def create_category(
    session: AsyncSession,
    *,
    business: Business,
    name: str,
    account_type: AccountType | str,
    description: str | None = None,
    parent_category_id: uuid.UUID | None = None,
) -> Category:
    try:
        account_type = AccountType(account_type)
    except ValueError:
        valid = ", ".join(t.value for t in AccountType)
        raise ValidationError(f"{account_type!r} is not an account type. One of: {valid}") from None

    if parent_category_id:
        parent = await get_category(session, parent_category_id)
        if parent.business_id != business.id:
            raise ValidationError("Parent category belongs to a different business")
        if parent.account_type is not account_type:
            raise ValidationError(
                f"A sub-account must share its parent's account type "
                f"({parent.name} is {parent.account_type.value})"
            )
    if await find_category_by_name(session, business_id=business.id, name=name):
        raise ValidationError(
            f"{business.name} already has a category named {name!r} "
            "(names are matched case-insensitively)"
        )
    category = Category(
        tenant_id=business.tenant_id,
        business_id=business.id,
        name=name,
        account_type=account_type,
        description=description,
        parent_category_id=parent_category_id,
    )
    session.add(category)
    await session.flush()
    return category


async def list_categories(
    session: AsyncSession,
    *,
    business_id: uuid.UUID,
    include_archived: bool = False,
    account_type: AccountType | str | None = None,
) -> Sequence[Category]:
    stmt = (
        select(Category)
        .where(Category.business_id == business_id)
        # Grouped by type, then alphabetically: how a chart of accounts reads.
        .order_by(Category.account_type, Category.name)
    )
    if not include_archived:
        stmt = stmt.where(Category.archived.is_(False))
    if account_type:
        stmt = stmt.where(Category.account_type == AccountType(account_type))
    return (await session.scalars(stmt)).all()


async def seed_chart_of_accounts(
    session: AsyncSession, *, business: Business
) -> tuple[list[Category], list[str]]:
    """Create the default chart of accounts for a business.

    Idempotent: names that already exist are skipped and returned, so running
    it against a partially-populated business tops it up instead of failing.
    """
    created: list[Category] = []
    skipped: list[str] = []
    for entry in DEFAULT_CHART:
        if await find_category_by_name(session, business_id=business.id, name=entry.name):
            skipped.append(entry.name)
            continue
        created.append(
            await create_category(
                session,
                business=business,
                name=entry.name,
                account_type=entry.account_type,
                description=entry.description,
            )
        )
    return created, skipped


async def get_category(session: AsyncSession, category_id: uuid.UUID) -> Category:
    category = await session.get(Category, category_id)
    if category is None:
        raise NotFoundError(f"Category {category_id} not found")
    return category


async def find_category_by_name(
    session: AsyncSession, *, business_id: uuid.UUID, name: str
) -> Category | None:
    return await session.scalar(
        select(Category).where(
            Category.business_id == business_id,
            func.lower(Category.name) == name.strip().lower(),
            Category.archived.is_(False),
        )
    )


# --- items --------------------------------------------------------------


async def create_item(
    session: AsyncSession,
    *,
    business: Business,
    provider: str,
    provider_item_id: str,
    access_token_encrypted: str,
    institution_name: str | None = None,
    backfill_start_date: date | None = None,
) -> Item:
    item = Item(
        tenant_id=business.tenant_id,
        business_id=business.id,
        provider=provider,
        provider_item_id=provider_item_id,
        access_token_encrypted=access_token_encrypted,
        institution_name=institution_name,
        backfill_start_date=backfill_start_date,
    )
    session.add(item)
    await session.flush()
    return item


async def get_item(session: AsyncSession, item_id: uuid.UUID) -> Item:
    item = await session.get(Item, item_id)
    if item is None:
        raise NotFoundError(f"Item {item_id} not found")
    return item


async def get_item_by_provider_id(
    session: AsyncSession, *, provider: str, provider_item_id: str
) -> Item | None:
    return await session.scalar(
        select(Item).where(Item.provider == provider, Item.provider_item_id == provider_item_id)
    )


async def list_items(
    session: AsyncSession, *, business_id: uuid.UUID | None = None, active_only: bool = False
) -> Sequence[Item]:
    stmt = select(Item).order_by(Item.created_at)
    if business_id:
        stmt = stmt.where(Item.business_id == business_id)
    if active_only:
        stmt = stmt.where(Item.status == "active")
    return (await session.scalars(stmt)).all()


# --- accounts -----------------------------------------------------------


async def upsert_account(session: AsyncSession, *, item: Item, raw: RawAccount) -> Account:
    account = await session.scalar(
        select(Account).where(
            Account.item_id == item.id, Account.provider_account_id == raw.provider_account_id
        )
    )
    if account is None:
        account = Account(
            tenant_id=item.tenant_id,
            business_id=item.business_id,
            item_id=item.id,
            provider_account_id=raw.provider_account_id,
        )
        session.add(account)
    account.name = raw.name
    account.account_type = raw.account_type
    account.classification = raw.classification
    account.current_balance = raw.current_balance
    await session.flush()
    return account


async def list_accounts(
    session: AsyncSession, *, business_id: uuid.UUID | None = None
) -> Sequence[Account]:
    stmt = select(Account).order_by(Account.name)
    if business_id:
        stmt = stmt.where(Account.business_id == business_id)
    return (await session.scalars(stmt)).all()


# --- transactions -------------------------------------------------------


@dataclass(frozen=True)
class TransactionFilters:
    business_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None
    needs_review: bool | None = None
    uncategorized: bool | None = None
    #: True: only transactions a human has reviewed. False: only ones nobody
    #: has. None (default): no filtering by review status at all.
    reviewed: bool | None = None
    start_date: date | None = None
    end_date: date | None = None
    search: str | None = None
    limit: int = 50
    offset: int = 0


def _apply_filters(stmt: Select, filters: TransactionFilters) -> Select:
    if filters.business_id:
        stmt = stmt.where(Transaction.business_id == filters.business_id)
    if filters.account_id:
        stmt = stmt.where(Transaction.account_id == filters.account_id)
    if filters.category_id:
        stmt = stmt.where(Transaction.category_id == filters.category_id)
    if filters.needs_review is not None:
        stmt = stmt.where(Transaction.needs_review.is_(filters.needs_review))
    if filters.reviewed is not None:
        stmt = stmt.where(
            Transaction.last_reviewed_at.is_not(None)
            if filters.reviewed
            else Transaction.last_reviewed_at.is_(None)
        )
    if filters.uncategorized:
        stmt = stmt.where(Transaction.category_id.is_(None))
    if filters.start_date:
        stmt = stmt.where(Transaction.date >= filters.start_date)
    if filters.end_date:
        stmt = stmt.where(Transaction.date <= filters.end_date)
    if filters.search:
        needle = f"%{filters.search.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Transaction.vendor).like(needle),
                func.lower(Transaction.description).like(needle),
                func.lower(Transaction.memo).like(needle),
            )
        )
    return stmt


async def list_transactions(
    session: AsyncSession, filters: TransactionFilters
) -> Sequence[Transaction]:
    stmt = _apply_filters(select(Transaction), filters)
    stmt = stmt.order_by(Transaction.date.desc(), Transaction.created_at.desc())
    stmt = stmt.limit(filters.limit).offset(filters.offset)
    return (await session.scalars(stmt)).all()


async def count_transactions(session: AsyncSession, filters: TransactionFilters) -> int:
    stmt = _apply_filters(select(func.count(Transaction.id)), filters)
    return int(await session.scalar(stmt) or 0)


async def get_transaction(session: AsyncSession, transaction_id: uuid.UUID) -> Transaction:
    transaction = await session.get(Transaction, transaction_id)
    if transaction is None:
        raise NotFoundError(f"Transaction {transaction_id} not found")
    return transaction


async def upsert_transaction(session: AsyncSession, *, values: dict) -> tuple[uuid.UUID, bool]:
    """Insert or update by ``(account_id, provider_transaction_id)``.

    Returns ``(transaction_id, is_new)``. Only provider-owned columns are
    overwritten on conflict — a human's category survives a re-sync.
    """
    provider_columns = {
        "amount",
        "date",
        "post_date",
        "vendor",
        "description",
        "memo",
        "transaction_type",
        "balance_after",
        "pending",
        "provider_category",
        "raw_payload",
    }
    statement: Any = (
        pg_insert(Transaction)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[Transaction.account_id, Transaction.provider_transaction_id],
            set_={k: v for k, v in values.items() if k in provider_columns},
        )
        # xmax = 0 is Postgres' idiom for "this row was inserted, not updated".
        .returning(Transaction.id, literal_column("(xmax = 0)").label("inserted"))
    )
    row = (await session.execute(statement)).one()
    return row[0], bool(row[1])


async def delete_transactions_by_provider_ids(
    session: AsyncSession, *, item_id: uuid.UUID, provider_transaction_ids: Sequence[str]
) -> int:
    if not provider_transaction_ids:
        return 0
    account_ids = select(Account.id).where(Account.item_id == item_id)
    result = cast(
        CursorResult,
        await session.execute(
            delete(Transaction).where(
                Transaction.account_id.in_(account_ids),
                Transaction.provider_transaction_id.in_(list(provider_transaction_ids)),
            )
        ),
    )
    return int(result.rowcount or 0)


async def find_similar_transactions(
    session: AsyncSession, *, transaction: Transaction, limit: int
) -> Sequence[Transaction]:
    """Categorized past transactions from the same business that look alike.

    Vendor match first; falls back to a description substring. Deliberately
    simple — swap for embeddings later without touching the agent's interface.
    """
    if limit <= 0:
        return []
    clauses = []
    if transaction.vendor:
        clauses.append(func.lower(Transaction.vendor) == transaction.vendor.lower())
    if transaction.description:
        token = transaction.description.strip().split(" ")[0].lower()
        if len(token) >= 4:
            clauses.append(func.lower(Transaction.description).like(f"{token}%"))
    if not clauses:
        return []

    stmt = (
        select(Transaction)
        .where(
            Transaction.business_id == transaction.business_id,
            Transaction.id != transaction.id,
            Transaction.category_id.is_not(None),
            or_(*clauses),
        )
        .order_by(Transaction.last_reviewed_at.desc().nullslast(), Transaction.date.desc())
        .limit(limit)
    )
    return (await session.scalars(stmt)).all()


# --- categorization history --------------------------------------------


async def list_history(
    session: AsyncSession, *, transaction_id: uuid.UUID, limit: int = 50
) -> Sequence[CategorizationHistory]:
    stmt = (
        select(CategorizationHistory)
        .where(CategorizationHistory.transaction_id == transaction_id)
        .order_by(CategorizationHistory.created_at.desc())
        .limit(limit)
    )
    return (await session.scalars(stmt)).all()


# --- rules --------------------------------------------------------------


async def create_rule(
    session: AsyncSession,
    *,
    business: Business,
    match_type: str,
    pattern: str,
    category_id: uuid.UUID,
    note: str | None = None,
    priority: int = 100,
    created_by: str | None = None,
) -> Rule:
    category = await get_category(session, category_id)
    if category.business_id != business.id:
        raise ValidationError("Rule category belongs to a different business")
    rule = Rule(
        tenant_id=business.tenant_id,
        business_id=business.id,
        match_type=match_type,
        pattern=pattern,
        category_id=category_id,
        note=note,
        priority=priority,
        created_by=created_by,
    )
    session.add(rule)
    await session.flush()
    return rule


async def list_rules(
    session: AsyncSession, *, business_id: uuid.UUID, active_only: bool = True
) -> Sequence[Rule]:
    stmt = (
        select(Rule).where(Rule.business_id == business_id).order_by(Rule.priority, Rule.created_at)
    )
    if active_only:
        stmt = stmt.where(Rule.active.is_(True))
    return (await session.scalars(stmt)).all()


async def get_rule(session: AsyncSession, rule_id: uuid.UUID) -> Rule:
    rule = await session.get(Rule, rule_id)
    if rule is None:
        raise NotFoundError(f"Rule {rule_id} not found")
    return rule


async def deactivate_rule(session: AsyncSession, rule_id: uuid.UUID) -> Rule:
    rule = await get_rule(session, rule_id)
    rule.active = False
    await session.flush()
    return rule
