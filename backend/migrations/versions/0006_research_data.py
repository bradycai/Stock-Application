"""Research data: companies, fundamentals (SEC EDGAR), filings, macro observations (FRED).

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-18
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('companies',
    sa.Column('symbol', sa.String(), nullable=False),
    sa.Column('cik', sa.String(), nullable=True),
    sa.Column('name', sa.String(), nullable=True),
    sa.Column('sic', sa.String(), nullable=True),
    sa.Column('sic_description', sa.String(), nullable=True),
    sa.Column('sector', sa.String(), nullable=True),
    sa.Column('industry', sa.String(), nullable=True),
    sa.Column('exchange', sa.String(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('symbol')
    )
    op.create_table('filings',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('symbol', sa.String(), nullable=False),
    sa.Column('cik', sa.String(), nullable=True),
    sa.Column('accession', sa.String(), nullable=False),
    sa.Column('form', sa.String(), nullable=False),
    sa.Column('filed_at', sa.Date(), nullable=True),
    sa.Column('period_of_report', sa.Date(), nullable=True),
    sa.Column('url', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('accession')
    )
    op.create_index(op.f('ix_filings_symbol'), 'filings', ['symbol'], unique=False)
    op.create_table('fundamentals',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('symbol', sa.String(), nullable=False),
    sa.Column('cik', sa.String(), nullable=True),
    sa.Column('metric', sa.String(), nullable=False),
    sa.Column('period_end', sa.Date(), nullable=False),
    sa.Column('fiscal_period', sa.String(), nullable=True),
    sa.Column('form', sa.String(), nullable=False),
    sa.Column('value', sa.Float(), nullable=False),
    sa.Column('unit', sa.String(), nullable=True),
    sa.Column('filed_at', sa.Date(), nullable=True),
    sa.Column('source', sa.String(), nullable=False),
    sa.Column('ingested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('symbol', 'metric', 'period_end', 'form', name='uq_fundamentals_key')
    )
    op.create_index(op.f('ix_fundamentals_symbol'), 'fundamentals', ['symbol'], unique=False)
    op.create_table('macro_observations',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('series_id', sa.String(), nullable=False),
    sa.Column('observation_date', sa.Date(), nullable=False),
    sa.Column('value', sa.Float(), nullable=True),
    sa.Column('realtime_start', sa.Date(), nullable=False),
    sa.Column('realtime_end', sa.Date(), nullable=True),
    sa.Column('source', sa.String(), nullable=False),
    sa.Column('ingested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('series_id', 'observation_date', 'realtime_start', name='uq_macro_obs_key')
    )
    op.create_index(op.f('ix_macro_observations_series_id'), 'macro_observations', ['series_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_macro_observations_series_id'), table_name='macro_observations')
    op.drop_table('macro_observations')
    op.drop_index(op.f('ix_fundamentals_symbol'), table_name='fundamentals')
    op.drop_table('fundamentals')
    op.drop_index(op.f('ix_filings_symbol'), table_name='filings')
    op.drop_table('filings')
    op.drop_table('companies')
