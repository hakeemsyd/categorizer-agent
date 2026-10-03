"""add vendor_key for merchant grouping

Revision ID: 4ec2d562d4e4
Revises: c8bae0a73535
Create Date: 2026-09-23 06:49:03.599520
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from books.core.vendors import normalize_vendor

revision = "4ec2d562d4e4"
down_revision = "c8bae0a73535"
branch_labels = None
depends_on = None

# Backfill in chunks: the normalizer is Python (too involved to restate
# faithfully in SQL), so existing rows are read and rewritten rather than
# updated in place by an expression.
_CHUNK = 1000


def upgrade() -> None:
    op.add_column("transactions", sa.Column("vendor_key", sa.Text(), nullable=True))
    op.create_index("ix_tx_vendor_key", "transactions", ["business_id", "vendor_key"], unique=False)

    connection = op.get_bind()
    offset = 0
    while True:
        rows = connection.execute(
            sa.text(
                "SELECT id, vendor, description FROM transactions "
                "ORDER BY id LIMIT :limit OFFSET :offset"
            ),
            {"limit": _CHUNK, "offset": offset},
        ).fetchall()
        if not rows:
            break
        updates = [
            {"row_id": row.id, "key": normalize_vendor(row.vendor, row.description)}
            for row in rows
        ]
        connection.execute(
            sa.text("UPDATE transactions SET vendor_key = :key WHERE id = :row_id"), updates
        )
        offset += _CHUNK


def downgrade() -> None:
    op.drop_index("ix_tx_vendor_key", table_name="transactions")
    op.drop_column("transactions", "vendor_key")
