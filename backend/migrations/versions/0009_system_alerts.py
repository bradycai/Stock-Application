"""Ops: system_alerts (health-watcher alerts, deduplicated by (kind, key) while active).

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0009'
down_revision: Union[str, Sequence[str], None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('system_alerts',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('kind', sa.String(), nullable=False),
    sa.Column('key', sa.String(), nullable=False),
    sa.Column('severity', sa.String(), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('first_seen', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_seen', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('count', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('acked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_system_alerts_status_last_seen', 'system_alerts', ['status', 'last_seen'],
                    unique=False)
    op.create_index('uq_system_alerts_active', 'system_alerts', ['kind', 'key'], unique=True,
                    postgresql_where=sa.text("status <> 'resolved'"))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('uq_system_alerts_active', table_name='system_alerts',
                  postgresql_where=sa.text("status <> 'resolved'"))
    op.drop_index('ix_system_alerts_status_last_seen', table_name='system_alerts')
    op.drop_table('system_alerts')
