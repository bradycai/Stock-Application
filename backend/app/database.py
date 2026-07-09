"""SQLAlchemy engine and session management."""
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency — yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Idempotent column additions for existing databases. create_all() creates missing
# TABLES but never adds columns to an existing table, so new columns on trade_intents /
# orders are applied here. Postgres supports ADD COLUMN IF NOT EXISTS. Safe to re-run.
_MIGRATIONS = [
    "ALTER TABLE trade_intents ADD COLUMN IF NOT EXISTS auto_executed BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE trade_intents ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS exit_reason VARCHAR",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS monitoring_status VARCHAR",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS last_checked_at TIMESTAMPTZ",
    "ALTER TABLE automation_state ADD COLUMN IF NOT EXISTS events_cleared_before TIMESTAMPTZ",
]


# Enum value additions must run OUTSIDE a transaction on older Postgres, so they go
# through an AUTOCOMMIT connection separately from the column migrations.
_ENUM_MIGRATIONS = [
    "ALTER TYPE intent_action ADD VALUE IF NOT EXISTS 'avoid'",
]


def run_migrations() -> None:
    from sqlalchemy import text

    with engine.begin() as conn:
        for stmt in _MIGRATIONS:
            conn.execute(text(stmt))

    with engine.connect() as conn:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        for stmt in _ENUM_MIGRATIONS:
            conn.execute(text(stmt))
