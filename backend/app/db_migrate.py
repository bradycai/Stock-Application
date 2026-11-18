"""Database migrations: `python -m app.db_migrate` (upgrade) or `--check` (report only).

Back up the database first (`pg_dump -Fc -d trading_assistant -f backups/<name>.dump`).

- Fresh database: upgrade to the latest revision.
- Pre-Alembic database (the app's tables exist but there is no `alembic_version`): apply
  the idempotent ALTER statements the app used to run at startup, so the schema matches
  the baseline revision, stamp it as the baseline, then upgrade to the latest revision.
- Database already managed by Alembic: upgrade to the latest revision.

The application never changes the schema at startup; it only checks that the database is
at the latest revision (see `schema_status`) and refuses to start otherwise.
"""
import argparse
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from app.config import settings

BACKEND_DIR = Path(__file__).resolve().parent.parent
BASELINE_REVISION = "0001"

# Every table the baseline revision creates. A pre-Alembic database must already have all
# of them (the old startup create_all() made them); otherwise we stop rather than guess.
BASELINE_TABLES = {
    "account_snapshots", "analysis_runs", "audit_log", "automation_state", "orders",
    "position_management", "risk_state", "trade_intents", "watchlist",
}

# The statements the app used to run on every startup (formerly database.run_migrations).
# All are idempotent; they bring any pre-Alembic database up to the baseline schema.
LEGACY_COLUMN_DDL = [
    "ALTER TABLE trade_intents ADD COLUMN IF NOT EXISTS auto_executed BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE trade_intents ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS exit_reason VARCHAR",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS monitoring_status VARCHAR",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS last_checked_at TIMESTAMPTZ",
    "ALTER TABLE automation_state ADD COLUMN IF NOT EXISTS events_cleared_before TIMESTAMPTZ",
]
# Enum additions must run outside a transaction on older PostgreSQL versions.
LEGACY_ENUM_DDL = [
    "ALTER TYPE intent_action ADD VALUE IF NOT EXISTS 'avoid'",
]


def alembic_config(database_url: str | None = None, *, configure_logging: bool = True) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    cfg.attributes["database_url"] = database_url or settings.database_url
    cfg.attributes["configure_logging"] = configure_logging
    return cfg


def head_revision(cfg: Config | None = None) -> str:
    return ScriptDirectory.from_config(cfg or alembic_config()).get_current_head()


def _current_revision(engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def _is_pre_alembic(engine) -> bool:
    tables = set(inspect(engine).get_table_names())
    return "alembic_version" not in tables and "trade_intents" in tables


def schema_status(database_url: str | None = None) -> dict:
    """Report the database's revision against the latest one. Never changes anything."""
    url = database_url or settings.database_url
    engine = create_engine(url, future=True)
    try:
        current = _current_revision(engine)
        legacy = _is_pre_alembic(engine)
    finally:
        engine.dispose()
    head = head_revision(alembic_config(url, configure_logging=False))
    return {"current": current, "head": head, "up_to_date": current == head, "pre_alembic": legacy}


def _bring_legacy_to_baseline(engine) -> None:
    missing = BASELINE_TABLES - set(inspect(engine).get_table_names())
    if missing:
        raise RuntimeError(
            f"Pre-Alembic database is missing baseline tables {sorted(missing)}; "
            "restore it from a backup or migrate it by hand. Nothing was changed."
        )
    with engine.begin() as conn:
        for stmt in LEGACY_COLUMN_DDL:
            conn.execute(text(stmt))
    with engine.connect() as conn:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        for stmt in LEGACY_ENUM_DDL:
            conn.execute(text(stmt))


def migrate(database_url: str | None = None, *, configure_logging: bool = True) -> dict:
    """Upgrade the database to the latest revision (see the module docstring)."""
    url = database_url or settings.database_url
    cfg = alembic_config(url, configure_logging=configure_logging)
    engine = create_engine(url, future=True)
    try:
        if _is_pre_alembic(engine):
            _bring_legacy_to_baseline(engine)
            command.stamp(cfg, BASELINE_REVISION)
    finally:
        engine.dispose()
    command.upgrade(cfg, "head")
    return schema_status(url)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="report the schema status only")
    args = parser.parse_args(argv)
    if args.check:
        status = schema_status()
        print(status)
        return 0 if status["up_to_date"] else 1
    status = migrate()
    print(status)
    return 0 if status["up_to_date"] else 1


if __name__ == "__main__":
    sys.exit(main())
