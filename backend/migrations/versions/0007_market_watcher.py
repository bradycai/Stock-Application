"""Market watcher: decisions, research job queue, worker heartbeats, per-symbol state.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0007'
down_revision: Union[str, Sequence[str], None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('watcher_decisions',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('symbol', sa.String(), nullable=False),
    sa.Column('action', sa.String(), nullable=False),
    sa.Column('event_type', sa.String(), nullable=True),
    sa.Column('urgency', sa.String(), nullable=False),
    sa.Column('evidence_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('explanation', sa.Text(), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('prompt_version', sa.String(), nullable=True),
    sa.Column('model', sa.String(), nullable=True),
    sa.Column('run_id', sa.String(), nullable=True),
    sa.Column('signal_event_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('downgraded', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_watcher_decisions_created_at'), 'watcher_decisions', ['created_at'], unique=False)
    op.create_index(op.f('ix_watcher_decisions_run_id'), 'watcher_decisions', ['run_id'], unique=False)
    op.create_index(op.f('ix_watcher_decisions_symbol'), 'watcher_decisions', ['symbol'], unique=False)
    op.create_table('research_jobs',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('job_key', sa.String(), nullable=False),
    sa.Column('kind', sa.String(), nullable=False),
    sa.Column('symbol', sa.String(), nullable=False),
    sa.Column('signal_event_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('decision_id', sa.BigInteger(), nullable=True),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('max_attempts', sa.Integer(), nullable=False),
    sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('leased_by', sa.String(), nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['decision_id'], ['watcher_decisions.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('job_key')
    )
    op.create_index(op.f('ix_research_jobs_status'), 'research_jobs', ['status'], unique=False)
    op.create_index(op.f('ix_research_jobs_symbol'), 'research_jobs', ['symbol'], unique=False)
    op.create_table('worker_heartbeats',
    sa.Column('worker', sa.String(), nullable=False),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_beat', sa.DateTime(timezone=True), nullable=False),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.PrimaryKeyConstraint('worker')
    )
    op.create_table('watcher_symbol_state',
    sa.Column('symbol', sa.String(), nullable=False),
    sa.Column('last_news_id', sa.BigInteger(), nullable=True),
    sa.Column('last_price', sa.Float(), nullable=True),
    sa.Column('last_price_ts', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_bar_ts', sa.DateTime(timezone=True), nullable=True),
    sa.Column('regime', sa.String(), nullable=True),
    sa.Column('position_fingerprint', sa.String(), nullable=True),
    sa.Column('last_assessed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_action', sa.String(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('symbol')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('watcher_symbol_state')
    op.drop_table('worker_heartbeats')
    op.drop_index(op.f('ix_research_jobs_symbol'), table_name='research_jobs')
    op.drop_index(op.f('ix_research_jobs_status'), table_name='research_jobs')
    op.drop_table('research_jobs')
    op.drop_index(op.f('ix_watcher_decisions_symbol'), table_name='watcher_decisions')
    op.drop_index(op.f('ix_watcher_decisions_run_id'), table_name='watcher_decisions')
    op.drop_index(op.f('ix_watcher_decisions_created_at'), table_name='watcher_decisions')
    op.drop_table('watcher_decisions')
