"""Add connected applications, credentials and administrative audit."""
from alembic import op
import sqlalchemy as sa

revision = 'd36be9142c08'
down_revision = '91ddb9426907'
branch_labels = None
depends_on = None


def timestamps():
    return [
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade():
    op.create_table(
        'connected_applications',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('name', sa.String(120), nullable=False),
        sa.Column('slug', sa.String(80), nullable=False, unique=True),
        sa.Column('destination_account_id', sa.Uuid(), nullable=False),
        sa.Column('website_url', sa.String(2048), nullable=False),
        sa.Column('key_hash', sa.String(64), nullable=False),
        sa.Column('key_prefix', sa.String(20), nullable=False),
        sa.Column('created_by', sa.String(255), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        *timestamps(),
    )
    op.create_table(
        'application_audit',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('application_id', sa.Uuid(), sa.ForeignKey('connected_applications.id'), nullable=False),
        sa.Column('actor_sub', sa.String(255), nullable=False),
        sa.Column('action', sa.String(40), nullable=False),
        *timestamps(),
    )
    op.create_index('ix_application_audit_application_id', 'application_audit', ['application_id'])
    op.add_column('checkout_sessions', sa.Column('application_id', sa.Uuid(), nullable=True))
    op.create_foreign_key('fk_checkout_application', 'checkout_sessions', 'connected_applications', ['application_id'], ['id'])
    op.create_index('ix_checkout_sessions_application_id', 'checkout_sessions', ['application_id'])


def downgrade():
    op.drop_index('ix_checkout_sessions_application_id', table_name='checkout_sessions')
    op.drop_constraint('fk_checkout_application', 'checkout_sessions', type_='foreignkey')
    op.drop_column('checkout_sessions', 'application_id')
    op.drop_table('application_audit')
    op.drop_table('connected_applications')
