"""initial schema

Revision ID: c8bae0a73535
Revises:
Create Date: 2026-09-21 03:03:25.844632
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c8bae0a73535"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # gen_random_uuid() is built in from Postgres 13; pgcrypto covers older
    # servers and is a no-op on Supabase, where it is already present.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.create_table(
        "tenants",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "businesses",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_businesses_tenant_name"),
    )
    op.create_table(
        "categories",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("business_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "account_type",
            sa.Enum("asset", "liability", "equity", "revenue", "expense", name="account_type"),
            nullable=False,
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("parent_category_id", sa.UUID(), nullable=True),
        sa.Column("archived", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_category_id"], ["categories.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_categories_business", "categories", ["business_id"], unique=False)
    op.create_index(
        "uq_categories_child_name",
        "categories",
        ["business_id", "parent_category_id", sa.literal_column("lower(name)")],
        unique=True,
        postgresql_where=sa.text("parent_category_id IS NOT NULL"),
    )
    op.create_index(
        "uq_categories_root_name",
        "categories",
        ["business_id", sa.literal_column("lower(name)")],
        unique=True,
        postgresql_where=sa.text("parent_category_id IS NULL"),
    )
    op.create_table(
        "items",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("business_id", sa.UUID(), nullable=False),
        sa.Column("provider", sa.Text(), server_default="plaid", nullable=False),
        sa.Column("provider_item_id", sa.Text(), nullable=True),
        sa.Column("access_token_encrypted", sa.Text(), nullable=True),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column("institution_name", sa.Text(), nullable=True),
        sa.Column("backfill_start_date", sa.Date(), nullable=True),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('active','error','disconnected')", name="ck_items_status"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "provider_item_id", name="uq_items_provider_item"),
    )
    op.create_index("ix_items_business", "items", ["business_id"], unique=False)
    op.create_table(
        "accounts",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("business_id", sa.UUID(), nullable=False),
        sa.Column("item_id", sa.UUID(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("provider_account_id", sa.Text(), nullable=True),
        sa.Column("account_type", sa.Text(), nullable=True),
        sa.Column(
            "classification",
            sa.Enum("asset", "liability", "equity", "revenue", "expense", name="account_type"),
            nullable=True,
        ),
        sa.Column("opening_balance", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("current_balance", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "classification IS NULL OR classification IN ('asset','liability')",
            name="ck_accounts_classification",
        ),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("item_id", "provider_account_id", name="uq_accounts_item_provider"),
    )
    op.create_index("ix_accounts_business", "accounts", ["business_id"], unique=False)
    op.create_table(
        "rules",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("business_id", sa.UUID(), nullable=False),
        sa.Column("match_type", sa.Text(), nullable=False),
        sa.Column("pattern", sa.Text(), nullable=False),
        sa.Column("category_id", sa.UUID(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("priority", sa.Integer(), server_default="100", nullable=False),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "match_type IN ('vendor_equals','vendor_contains','description_contains')",
            name="ck_rules_match_type",
        ),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_rules_business_active", "rules", ["business_id", "active", "priority"], unique=False
    )
    op.create_table(
        "transactions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("business_id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("category_id", sa.UUID(), nullable=True),
        sa.Column("provider_transaction_id", sa.Text(), nullable=True),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("post_date", sa.Date(), nullable=True),
        sa.Column("vendor", sa.Text(), nullable=True),
        sa.Column("customer", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("memo", sa.Text(), nullable=True),
        sa.Column("transaction_type", sa.Text(), nullable=True),
        sa.Column("balance_after", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("pending", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "entry_side",
            sa.Enum("debit", "credit", name="entry_side"),
            sa.Computed(
                "CASE WHEN amount < 0 THEN 'debit'::entry_side ELSE 'credit'::entry_side END",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.Column("provider_category", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("needs_review", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("last_reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "provider_transaction_id", name="uq_tx_account_provider"),
    )
    op.create_index("ix_tx_business_date", "transactions", ["business_id", "date"], unique=False)
    op.create_index(
        "ix_tx_needs_review", "transactions", ["business_id", "needs_review"], unique=False
    )
    op.create_index("ix_tx_vendor", "transactions", ["business_id", "vendor"], unique=False)
    op.create_table(
        "categorization_history",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("transaction_id", sa.UUID(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("category_id", sa.UUID(), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_history_transaction",
        "categorization_history",
        ["transaction_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_history_transaction", table_name="categorization_history")
    op.drop_table("categorization_history")
    op.drop_index("ix_tx_vendor", table_name="transactions")
    op.drop_index("ix_tx_needs_review", table_name="transactions")
    op.drop_index("ix_tx_business_date", table_name="transactions")
    op.drop_table("transactions")
    op.drop_index("ix_rules_business_active", table_name="rules")
    op.drop_table("rules")
    op.drop_index("ix_accounts_business", table_name="accounts")
    op.drop_table("accounts")
    op.drop_index("ix_items_business", table_name="items")
    op.drop_table("items")
    op.drop_index(
        "uq_categories_root_name",
        table_name="categories",
        postgresql_where=sa.text("parent_category_id IS NULL"),
    )
    op.drop_index(
        "uq_categories_child_name",
        table_name="categories",
        postgresql_where=sa.text("parent_category_id IS NOT NULL"),
    )
    op.drop_index("ix_categories_business", table_name="categories")
    op.drop_table("categories")
    op.drop_table("businesses")
    op.drop_table("tenants")
    op.execute("DROP TYPE IF EXISTS entry_side")
    op.execute("DROP TYPE IF EXISTS account_type")
