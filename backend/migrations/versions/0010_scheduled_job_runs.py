"""Recurring jobs: scheduled_job_runs (idempotent per (job_name, period_key)).

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-20
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0010'
down_revision: Union[str, Sequence[str], None] = '0009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('scheduled_job_runs',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('job_name', sa.String(), nullable=False),
    sa.Column('period_key', sa.String(), nullable=False),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('job_name', 'period_key', name='uq_scheduled_job_runs_period')
    )
    op.create_index('ix_scheduled_job_runs_job_started', 'scheduled_job_runs', ['job_name', 'started_at'],
                    unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_scheduled_job_runs_job_started', table_name='scheduled_job_runs')
    op.drop_table('scheduled_job_runs')
