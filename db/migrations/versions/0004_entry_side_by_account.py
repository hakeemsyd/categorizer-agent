"""entry_side must account for credit cards, not just the sign

Revision ID: 9a3c71f0be25
Revises: 4ec2d562d4e4
Create Date: 2026-09-23 17:40:00.000000

``entry_side`` was generated from ``amount < 0``. That is only right for asset
accounts. A credit card is a liability whose balance grows as you spend, so
Teller reports a purchase as a *positive* amount — and every one of those was
being booked as money in. On the Platinum Card in the development data that is
101 of 110 rows.

The correct derivation needs the account's classification, which a generated
column cannot reach across tables for, so the column becomes an ordinary one
written by ``repository.upsert_transaction``.
"""

from __future__ import annotations

from alembic import op

from books.core.models import ENTRY_SIDE_FUNCTION, ENTRY_SIDE_TRIGGER

revision = "9a3c71f0be25"
down_revision = "4ec2d562d4e4"
branch_labels = None
depends_on = None

# Money out is negative on an asset account and positive on a liability one;
# the category then takes the opposite side from the bank account. Zero is a
# credit either way, matching accounting.entry_side_for_amount.
_CORRECT_SIDE = """
CASE
    WHEN a.classification = 'liability' THEN
        CASE WHEN t.amount > 0 THEN 'debit'::entry_side ELSE 'credit'::entry_side END
    ELSE
        CASE WHEN t.amount < 0 THEN 'debit'::entry_side ELSE 'credit'::entry_side END
END
"""


def upgrade() -> None:
    # DROP EXPRESSION keeps the column and its data in place; only the
    # generating rule goes away. The values are then corrected below.
    op.execute("ALTER TABLE transactions ALTER COLUMN entry_side DROP EXPRESSION")

    # The rule moves from a generated expression to a trigger, which can read
    # the account. Imported rather than restated so the migration and the
    # schema the models build cannot drift apart.
    op.execute(ENTRY_SIDE_FUNCTION)
    op.execute(ENTRY_SIDE_TRIGGER)
    op.execute(
        f"""
        UPDATE transactions t
           SET entry_side = {_CORRECT_SIDE}
          FROM accounts a
         WHERE a.id = t.account_id
        """
    )
    # transaction_type mirrored the same faulty reading of the sign.
    op.execute(
        """
        UPDATE transactions
           SET transaction_type = entry_side::text
         WHERE transaction_type IN ('debit', 'credit')
        """
    )


def downgrade() -> None:
    # A generated column cannot be added back in place, so the column is
    # rebuilt. This restores the old behaviour, mis-signed card rows included.
    op.execute("DROP TRIGGER IF EXISTS transactions_entry_side ON transactions")
    op.execute("DROP FUNCTION IF EXISTS transactions_set_entry_side()")
    op.execute("ALTER TABLE transactions DROP COLUMN entry_side")
    op.execute(
        """
        ALTER TABLE transactions ADD COLUMN entry_side entry_side
        GENERATED ALWAYS AS (
            CASE WHEN amount < 0 THEN 'debit'::entry_side ELSE 'credit'::entry_side END
        ) STORED NOT NULL
        """
    )
