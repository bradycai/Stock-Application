"""/health and /health/ready. No broker or Claude calls; schema_status is stubbed or run
against the disposable test database."""
import os

import pytest
from alembic import command
from fastapi.testclient import TestClient

from app import db_migrate
from app.main import app

_orig_schema_status = db_migrate.schema_status
client = TestClient(app)  # no context manager: startup hooks (monitor loop) do not run


def test_liveness_unchanged():
    r = client.get("/health")
    assert r.status_code == 200 and set(r.json()) == {"status", "trading_mode"}


def test_ready_ok(monkeypatch):
    monkeypatch.setattr(db_migrate, "schema_status",
                        lambda: {"current": "0009", "head": "0009", "up_to_date": True})
    r = client.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["ready"] is True and r.json()["checks"]["database"] is True


def test_ready_503_when_schema_behind(monkeypatch):
    monkeypatch.setattr(db_migrate, "schema_status",
                        lambda: {"current": "0008", "head": "0009", "up_to_date": False})
    r = client.get("/health/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["ready"] is False and body["checks"]["schema_up_to_date"] is False


def test_ready_503_and_redacted_when_database_unreachable(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "api_token", "TOPSECRETTOKEN99")

    def boom():
        raise RuntimeError("could not connect using TOPSECRETTOKEN99")
    monkeypatch.setattr(db_migrate, "schema_status", boom)
    r = client.get("/health/ready")
    assert r.status_code == 503
    assert r.json()["ready"] is False and r.json()["checks"]["database"] is False
    assert "TOPSECRETTOKEN99" not in r.text


def test_ready_503_on_unknown_status_shape(monkeypatch):
    monkeypatch.setattr(db_migrate, "schema_status", lambda: {})  # missing up_to_date: fail closed
    assert client.get("/health/ready").status_code == 503


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")
def test_ready_against_real_schema_behind_and_at_head(pg_db, monkeypatch):
    url = os.environ["TEST_DATABASE_URL"]
    monkeypatch.setattr(db_migrate, "schema_status", lambda: _orig_schema_status(url))
    assert client.get("/health/ready").status_code == 200
    command.downgrade(db_migrate.alembic_config(url, configure_logging=False), "0008")
    r = client.get("/health/ready")
    assert r.status_code == 503 and r.json()["checks"]["schema_revision"] == "0008"

