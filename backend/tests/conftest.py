"""Shared fixtures.

`pg_db` gives a SQLAlchemy session on a disposable PostgreSQL database migrated to the
latest Alembic revision. It needs TEST_DATABASE_URL naming a database that contains
"test" (CI provides one); the public schema is DROPPED first. Tests using it are skipped
when TEST_DATABASE_URL is not set.
"""
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture()
def pg_db():
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    if "test" not in (make_url(TEST_DATABASE_URL).database or ""):
        pytest.fail("TEST_DATABASE_URL must name a disposable database containing 'test'")
    from alembic import command

    from app import db_migrate

    engine = create_engine(TEST_DATABASE_URL, future=True)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    command.upgrade(db_migrate.alembic_config(TEST_DATABASE_URL, configure_logging=False), "head")
    session = sessionmaker(bind=engine, future=True)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
