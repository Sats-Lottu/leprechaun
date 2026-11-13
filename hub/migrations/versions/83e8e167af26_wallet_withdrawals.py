"""Persist withdrawal intent before calling the payment provider."""

import sqlalchemy as sa
from alembic import op

revision = '83e8e167af26'
down_revision = 'd36be9142c08'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'wallet_withdrawals',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column('user_sub', sa.String(255), nullable=False),
        sa.Column('invoice_digest', sa.String(64), nullable=False),
        sa.Column('payment_request', sa.String(4096), nullable=False),
        sa.Column('amount_msat', sa.Integer(), nullable=False),
        sa.Column('account_id', sa.Uuid(), nullable=False),
        sa.Column('hold_id', sa.Uuid()),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('payment_hash', sa.String(160), nullable=False),
        sa.Column('checking_id', sa.String(160), nullable=False),
        sa.UniqueConstraint(
            'user_sub',
            'invoice_digest',
            name='uq_wallet_withdrawals_user_invoice',
        ),
    )
    op.create_index(
        'ix_wallet_withdrawals_status', 'wallet_withdrawals', ['status']
    )


def downgrade():
    op.drop_table('wallet_withdrawals')
