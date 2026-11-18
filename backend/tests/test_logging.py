"""Structured logging and secret redaction."""
import json
import logging

import pytest

from app import logging_config as lc
from app.config import settings

SECRET_KEY = "sk-ant-test-super-secret-value"
ALPACA_SECRET = "alpaca-secret-abcdef123456"


@pytest.fixture(autouse=True)
def fake_secrets(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", SECRET_KEY)
    monkeypatch.setattr(settings, "alpaca_secret", ALPACA_SECRET)
    monkeypatch.setattr(settings, "database_url", "postgresql+psycopg://u:db-password-xyz@h:5432/d")


def _record(msg, **extra):
    rec = logging.LogRecord("trading.test", logging.INFO, __file__, 1, msg, None, None)
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def test_redact_replaces_every_configured_secret():
    text = f"key={SECRET_KEY} alpaca={ALPACA_SECRET} url=postgresql://u:db-password-xyz@h/d"
    out = lc.redact(text)
    assert SECRET_KEY not in out and ALPACA_SECRET not in out and "db-password-xyz" not in out
    assert out.count("***") == 3


def test_short_placeholder_values_are_not_treated_as_secrets(monkeypatch):
    monkeypatch.setattr(settings, "api_token", "abc")
    assert lc.redact("abc and abcdef") == "abc and abcdef"


def test_json_formatter_emits_fields_and_redacts_them():
    rec = _record("order.submitted", ticker="AAPL", order_id=7, note=f"leaked {SECRET_KEY}")
    payload = json.loads(lc.JsonFormatter().format(rec))
    assert payload["msg"] == "order.submitted"
    assert payload["ticker"] == "AAPL" and payload["order_id"] == 7
    assert SECRET_KEY not in json.dumps(payload)


def test_run_id_from_context_is_attached():
    token = lc.run_id_var.set("run-123")
    try:
        payload = json.loads(lc.JsonFormatter().format(_record("analysis.start")))
    finally:
        lc.run_id_var.reset(token)
    assert payload["run_id"] == "run-123"


def test_text_formatter_is_key_value_and_redacted():
    line = lc.TextFormatter().format(_record("claude.call", model="m", secret=ALPACA_SECRET))
    assert "claude.call" in line and 'model="m"' in line and ALPACA_SECRET not in line


def test_log_event_ignores_reserved_field_names(caplog):
    log = logging.getLogger("trading.test")
    with caplog.at_level(logging.INFO, logger="trading.test"):
        lc.log_event(log, "x.event", name="clash", ticker="MSFT")  # 'name' is a LogRecord attr
    rec = caplog.records[-1]
    assert rec.getMessage() == "x.event" and rec.ticker == "MSFT"


def test_configure_logging_is_idempotent():
    lc.configure_logging(level="INFO", fmt="json")
    lc.configure_logging(level="INFO", fmt="json")
    ours = [h for h in logging.getLogger().handlers if getattr(h, "_trading_handler", False)]
    assert len(ours) == 1


def test_encoded_forms_of_secrets_are_redacted(monkeypatch):
    monkeypatch.setattr(settings, "database_url", 'postgresql+psycopg://u:p%40ss"w0rd@h:5432/d')
    out = lc.redact("url=postgresql://u:p%40ss%22w0rd@h/d")
    assert "p%40ss%22w0rd" not in out
    rec = _record("x", note='quote p@ss"w0rd here')
    assert 'p@ss' not in lc.JsonFormatter().format(rec)


def test_safe_error_redacts_before_truncating():
    msg = "x" * 290 + SECRET_KEY  # the secret straddles a 300-char cut
    out = lc.safe_error(msg, 300)
    assert SECRET_KEY[:10] not in out
