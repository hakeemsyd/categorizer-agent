"""SQLAlchemy models — the source of truth for the schema.

Shape follows plan.md §4. Two-level ownership: a tenant owns businesses, and
``tenant_id`` is denormalized onto every table so Supabase RLS policies can
filter without a join when multi-tenant auth lands.
"""

from __future__ import annotations

import datetime as dt
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    DDL,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    FetchedValue,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    literal_column,
    text,
)
from sqlalchemy import event as sa_event
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from books.core.accounting import AccountType, EntrySide, normal_balance


class Base(DeclarativeBase):
    pass


def _enum(python_enum: type, name: str) -> Enum:
    """A native Postgres enum storing the member *values*, not their names."""
    return Enum(
        python_enum,
        name=name,
        native_enum=True,
        values_callable=lambda members: [m.value for m in members],
    )


ACCOUNT_TYPE = _enum(AccountType, "account_type")
ENTRY_SIDE = _enum(EntrySide, "entry_side")


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()

    businesses: Mapped[list[Business]] = relationship(back_populates="tenant")


class Business(Base):
    __tablename__ = "businesses"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()

    tenant: Mapped[Tenant] = relationship(back_populates="businesses")

    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_businesses_tenant_name"),)


class Category(Base):
    """A line in a business's chart of accounts."""

    __tablename__ = "categories"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # Which of the five fundamentals this line of the chart of accounts is.
    # Not derivable from the name, and everything downstream depends on it.
    account_type: Mapped[AccountType] = mapped_column(ACCOUNT_TYPE, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    parent_category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("categories.id", ondelete="SET NULL")
    )
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime] = _created_at()

    # A plain UNIQUE over (business_id, name, parent_category_id) would not
    # stop duplicate top-level names: Postgres treats NULL parents as
    # distinct. Two partial indexes over lower(name) close that, and make
    # uniqueness agree with the case-insensitive lookup the agent uses.
    @property
    def normal_balance(self) -> EntrySide:
        """The side that increases this account. Derived, never stored."""
        return normal_balance(self.account_type)

    __table_args__ = (
        Index(
            "uq_categories_root_name",
            "business_id",
            func.lower(literal_column("name")),
            unique=True,
            postgresql_where=text("parent_category_id IS NULL"),
        ),
        Index(
            "uq_categories_child_name",
            "business_id",
            "parent_category_id",
            func.lower(literal_column("name")),
            unique=True,
            postgresql_where=text("parent_category_id IS NOT NULL"),
        ),
        Index("ix_categories_business", "business_id"),
    )


class Item(Base):
    """One provider connection (an enrollment/item, in the aggregator's own terms)."""

    __tablename__ = "items"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    # Per-item, not global, so more than one aggregator can be in use at once.
    # No server default on purpose: every insert path sets this explicitly
    # (repo.create_item), and a stored default just goes stale the next time
    # the active provider changes — which is exactly what happened here.
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    provider_item_id: Mapped[str | None] = mapped_column(Text)
    access_token_encrypted: Mapped[str | None] = mapped_column(Text)
    cursor: Mapped[str | None] = mapped_column(Text)
    institution_name: Mapped[str | None] = mapped_column(Text)
    backfill_start_date: Mapped[dt.date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    last_error: Mapped[str | None] = mapped_column(Text)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint("provider", "provider_item_id", name="uq_items_provider_item"),
        CheckConstraint("status IN ('active','error','disconnected')", name="ck_items_status"),
        Index("ix_items_business", "business_id"),
    )


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("items.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    provider_account_id: Mapped[str | None] = mapped_column(Text)
    account_type: Mapped[str | None] = mapped_column(Text)
    # An account holds cash (asset) or owes money (liability). Same vocabulary
    # as a category's account_type, so reports do not need a translation table.
    classification: Mapped[AccountType | None] = mapped_column(ACCOUNT_TYPE)
    opening_balance: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    current_balance: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        CheckConstraint(
            "classification IS NULL OR classification IN ('asset','liability')",
            name="ck_accounts_classification",
        ),
        UniqueConstraint("item_id", "provider_account_id", name="uq_accounts_item_provider"),
        Index("ix_accounts_business", "business_id"),
    )


class Transaction(Base):
    __tablename__ = "transactions"
    # entry_side is computed by Postgres, so SQLAlchemy expires it after every
    # flush. eager_defaults fetches it back via RETURNING in the same statement
    # rather than emitting a lazy SELECT, which would fail under asyncio.
    # RUF012 wants a ClassVar annotation here, but SQLAlchemy declares
    # __mapper_args__ as an instance attribute and mypy rejects the override.
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("categories.id", ondelete="SET NULL")
    )

    provider_transaction_id: Mapped[str | None] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    post_date: Mapped[dt.date | None] = mapped_column(Date)
    vendor: Mapped[str | None] = mapped_column(Text)
    # Normalized grouping key for the merchant (books.core.vendors), so one
    # human decision can cover every transaction from the same place. Written
    # at ingestion; null when the row names no merchant at all (a bare wire).
    vendor_key: Mapped[str | None] = mapped_column(Text)
    customer: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    memo: Mapped[str | None] = mapped_column(Text)
    transaction_type: Mapped[str | None] = mapped_column(Text)
    balance_after: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    pending: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    # The side this transaction applies to its category; the bank account takes
    # the other one. Written by a database trigger (ENTRY_SIDE_TRIGGER below),
    # never by application code, so no write path can put it out of sync with
    # the amount — the same guarantee the old generated column gave, but a
    # correct one: it reads the account's classification, which a generated
    # column cannot reach across tables for.
    entry_side: Mapped[EntrySide] = mapped_column(
        ENTRY_SIDE, server_default=FetchedValue(), nullable=False
    )

    # Raw provider suggestion. Informational only — never written to category_id.
    provider_category: Mapped[str | None] = mapped_column(Text)
    # The agent's confidence in the CURRENT category_id.
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # Only ever set by an explicit human action, never by the agent.
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("account_id", "provider_transaction_id", name="uq_tx_account_provider"),
        Index("ix_tx_business_date", "business_id", "date"),
        Index("ix_tx_needs_review", "business_id", "needs_review"),
        Index("ix_tx_vendor", "business_id", "vendor"),
        Index("ix_tx_vendor_key", "business_id", "vendor_key"),
    )


class CategorizationHistory(Base):
    """Append-only audit of every category write, whoever made it."""

    __tablename__ = "categorization_history"

    id: Mapped[uuid.UUID] = _pk()
    # clock_timestamp(), not now(): two writes inside one transaction (an agent
    # guess then a human correction) must still order correctly.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False
    )
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("categories.id", ondelete="SET NULL")
    )
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    rationale: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_history_transaction", "transaction_id", "created_at"),)


class Rule(Base):
    """A standing human instruction the categorizer must honour.

    Not in plan.md §4's DDL, but plan.md §2 routes and §6 ("standing rules")
    both call for it. Deterministic matches short-circuit the model entirely.
    """

    __tablename__ = "rules"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    # 'vendor_equals' | 'vendor_contains' | 'description_contains'
    match_type: Mapped[str] = mapped_column(Text, nullable=False)
    pattern: Mapped[str] = mapped_column(Text, nullable=False)
    category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("categories.id", ondelete="CASCADE"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="100")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        CheckConstraint(
            "match_type IN ('vendor_equals','vendor_contains','description_contains')",
            name="ck_rules_match_type",
        ),
        Index("ix_rules_business_active", "business_id", "active", "priority"),
    )


# --- entry_side: derived in the database, from the account it belongs to ----
#
# The rule is stated in Python in books.core.accounting.entry_side_for_amount
# and enforced here in SQL. Two statements of one rule is a drift risk, so
# tests/integration/test_accounting_schema.py asserts the database agrees with
# the Python across the whole (classification x sign) matrix.
#
# It fires on every UPDATE, not just UPDATE OF amount, so that no statement can
# set entry_side by hand. That costs one indexed single-row lookup per update
# — worth it for a column the books balance on.
ENTRY_SIDE_FUNCTION = """
CREATE OR REPLACE FUNCTION transactions_set_entry_side() RETURNS trigger AS $$
DECLARE
    bank_account_type account_type;
BEGIN
    SELECT a.classification INTO bank_account_type
      FROM accounts a WHERE a.id = NEW.account_id;

    -- A credit card is a liability: spending on it is a POSITIVE amount,
    -- because it increases what the business owes. Everywhere else, money
    -- leaving is negative. Either way the category takes the opposite side
    -- from the bank account, and a zero amount is a credit.
    IF bank_account_type = 'liability' THEN
        NEW.entry_side := CASE
            WHEN NEW.amount > 0 THEN 'debit'::entry_side ELSE 'credit'::entry_side END;
    ELSE
        NEW.entry_side := CASE
            WHEN NEW.amount < 0 THEN 'debit'::entry_side ELSE 'credit'::entry_side END;
    END IF;

    -- A plain-text mirror of the same fact. Kept from the same source so the
    -- two cannot disagree, as they did when each read the sign separately.
    NEW.transaction_type := NEW.entry_side::text;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

ENTRY_SIDE_TRIGGER = """
CREATE TRIGGER transactions_entry_side
BEFORE INSERT OR UPDATE ON transactions
FOR EACH ROW EXECUTE FUNCTION transactions_set_entry_side();
"""

# Attached to the table so create_all() — which tests use instead of running
# the migrations — produces a schema that behaves like production.
sa_event.listen(Transaction.__table__, "after_create", DDL(ENTRY_SIDE_FUNCTION))
sa_event.listen(Transaction.__table__, "after_create", DDL(ENTRY_SIDE_TRIGGER))
