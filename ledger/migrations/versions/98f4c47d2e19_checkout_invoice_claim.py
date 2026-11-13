"""Track which checkout consumed each confirmed invoice."""

import sqlalchemy as sa
from alembic import op

revision = '98f4c47d2e19'
down_revision = 'cd27e356965c'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'ledger_transactions', sa.Column('checkout_invoice_claim', sa.Uuid())
    )
    op.add_column(
        'ledger_transactions',
        sa.Column('external_paid_at', sa.DateTime(timezone=True)),
    )
    op.create_index(
        'ix_ledger_entries_statement',
        'ledger_entries',
        ['account_id', 'created_at', 'id'],
    )


def downgrade():
    op.drop_index('ix_ledger_entries_statement', table_name='ledger_entries')
    op.drop_column('ledger_transactions', 'external_paid_at')
    op.drop_column('ledger_transactions', 'checkout_invoice_claim')
