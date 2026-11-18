"""Alembic migration tests.

They need a disposable PostgreSQL database named with "test" (CI provides one via
TEST_DATABASE_URL). The database's public schema is DROPPED and recreated by these tests,
so they refuse to run against any database whose name doesn't contain "test".
"""
import os

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

import app.models  # noqa: F401  (register tables)
from app import db_migrate
from app.database import Base

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture()
def engine():
    if "test" not in (make_url(URL).database or ""):
        pytest.fail("TEST_DATABASE_URL must name a disposable database containing 'test'")
    eng = create_engine(URL, future=True)
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    yield eng
    eng.dispose()


def _cfg():
    return db_migrate.alembic_config(URL, configure_logging=False)


def test_fresh_upgrade_matches_models_and_round_trips(engine):
    command.upgrade(_cfg(), "head")
    with engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []

    command.downgrade(_cfg(), "base")
    assert set(inspect(engine).get_table_names()) <= {"alembic_version"}

    command.upgrade(_cfg(), "head")
    assert db_migrate.schema_status(URL)["up_to_date"] is True


def test_pre_alembic_database_is_brought_to_baseline_stamped_and_upgraded(engine):
    # Simulate a database created by the old app: baseline tables, no alembic_version,
    # and a column the old startup ALTERs would have added still missing.
    command.upgrade(_cfg(), db_migrate.BASELINE_REVISION)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE alembic_version"))
        conn.execute(text("ALTER TABLE orders DROP COLUMN exit_reason"))
        conn.execute(text("INSERT INTO watchlist (ticker, active) VALUES ('AAPL', true)"))
    assert db_migrate.schema_status(URL)["pre_alembic"] is True

    status = db_migrate.migrate(URL, configure_logging=False)

    assert status["up_to_date"] is True
    assert "exit_reason" in {c["name"] for c in inspect(engine).get_columns("orders")}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT ticker FROM watchlist")).scalar_one() == "AAPL"


def test_pre_alembic_database_missing_tables_is_left_untouched(engine):
    command.upgrade(_cfg(), db_migrate.BASELINE_REVISION)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE alembic_version"))
        conn.execute(text("DROP TABLE risk_state"))

    with pytest.raises(RuntimeError, match="missing baseline tables"):
        db_migrate.migrate(URL, configure_logging=False)
    assert "alembic_version" not in inspect(engine).get_table_names()


def test_startup_check_reports_out_of_date_schema(engine):
    command.upgrade(_cfg(), db_migrate.BASELINE_REVISION)
    status = db_migrate.schema_status(URL)
    assert status["current"] == db_migrate.BASELINE_REVISION
    assert status["up_to_date"] is (db_migrate.head_revision() == db_migrate.BASELINE_REVISION)
