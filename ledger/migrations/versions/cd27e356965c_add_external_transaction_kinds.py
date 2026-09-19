"""add external transaction kinds

Revision ID: cd27e356965c
Revises: 56c79bc45c98
Create Date: 2026-09-19 10:35:40.453495

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'cd27e356965c'
down_revision: Union[str, Sequence[str], None] = '56c79bc45c98'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('ledger_transactions') as batch_op:
        batch_op.add_column(
            sa.Column(
                'kind',
                sa.String(),
                nullable=False,
                server_default='transfer',
            )
        )
        batch_op.add_column(
            sa.Column(
                'external_origin',
                sa.String(length=50),
                nullable=False,
                server_default='',
            )
        )
        batch_op.create_check_constraint(
            'ck_ledger_transactions_kind_valid',
            "kind IN ('transfer', 'external_credit', 'external_debit')",
        )
        batch_op.create_index(
            op.f('ix_ledger_transactions_external_origin'),
            ['external_origin'],
            unique=False,
        )
        batch_op.create_index(
            op.f('ix_ledger_transactions_kind'),
            ['kind'],
            unique=False,
        )

    with op.batch_alter_table('ledger_transactions') as batch_op:
        batch_op.alter_column('kind', server_default=None)
        batch_op.alter_column('external_origin', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('ledger_transactions') as batch_op:
        batch_op.drop_index(op.f('ix_ledger_transactions_kind'))
        batch_op.drop_index(op.f('ix_ledger_transactions_external_origin'))
        batch_op.drop_constraint(
            'ck_ledger_transactions_kind_valid', type_='check'
        )
        batch_op.drop_column('external_origin')
        batch_op.drop_column('kind')
