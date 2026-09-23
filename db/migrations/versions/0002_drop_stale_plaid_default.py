"""drop stale plaid default on items provider

Revision ID: 16f9a1588f7c
Revises: c8bae0a73535
Create Date: 2026-09-21 07:11:31.087504
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "16f9a1588f7c"
down_revision = "c8bae0a73535"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "items", "provider", existing_type=sa.TEXT(), server_default=None, existing_nullable=False
    )


def downgrade() -> None:
    op.alter_column(
        "items",
        "provider",
        existing_type=sa.TEXT(),
        server_default=sa.text("'plaid'::text"),
        existing_nullable=False,
    )
