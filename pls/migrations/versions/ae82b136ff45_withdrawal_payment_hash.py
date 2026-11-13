"""Allow a BOLT11 payment hash to back a single withdrawal."""

import sqlalchemy as sa
from alembic import op

revision = 'ae82b136ff45'
down_revision = '5bd7b91a50f0'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('lightning_invoices') as batch:
        batch.add_column(sa.Column('withdrawal_payment_hash', sa.String(160)))
        batch.create_unique_constraint(
            'uq_lightning_invoices_withdrawal_hash',
            ['withdrawal_payment_hash'],
        )


def downgrade():
    with op.batch_alter_table('lightning_invoices') as batch:
        batch.drop_constraint(
            'uq_lightning_invoices_withdrawal_hash', type_='unique'
        )
        batch.drop_column('withdrawal_payment_hash')
