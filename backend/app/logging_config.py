"""Central logging: INFO-level structured logs with secret redaction.

`configure_logging()` is called once when the app is imported. Every log line is either a
JSON object (LOG_FORMAT=json, the default) or `key=value` text (LOG_FORMAT=text), and
carries any structured fields passed through `log_event(...)` — run_id, ticker, model,
elapsed_ms, tokens, decision, risk result, order id, and so on.

Secrets are never logged: every configured secret value (Anthropic key, Alpaca key and
secret, API token, NewsAPI key, database password) is replaced with `***` in the final
rendered line, whatever field or message it appeared in.
"""
import contextvars
import json
import logging
import sys
from datetime import datetime, timezone

from sqlalchemy.engine import make_url

from app.config import settings

# The current analysis/monitor run, attached to every log line emitted while it is set.
run_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("run_id", default=None)

_RESERVED = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}
_REDACTED = "***"
_MIN_SECRET_LEN = 6  # never "redact" trivially short placeholder values


def _secret_values() -> list[str]:
    values = [
        settings.anthropic_api_key,
        settings.alpaca_key,
        settings.alpaca_secret,
        settings.api_token,
        settings.news_api_key,
    ]
    try:
        values.append(make_url(settings.database_url).password or "")
    except Exception:  # noqa: BLE001 - a malformed URL must not break logging
        pass
    # Longest first so a secret that contains another is fully replaced.
    return sorted({v for v in values if v and len(v) >= _MIN_SECRET_LEN}, key=len, reverse=True)


def redact(text: str) -> str:
    for secret in _secret_values():
        text = text.replace(secret, _REDACTED)
    return text


def _fields(record: logging.LogRecord) -> dict:
    fields = {k: v for k, v in record.__dict__.items() if k not in _RESERVED and not k.startswith("_")}
    run_id = fields.pop("run_id", None) or run_id_var.get()
    if run_id:
        fields = {"run_id": run_id, **fields}
    return fields


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            **_fields(record),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, default=str))


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        parts = [f"{ts}Z {record.levelname:<7} {record.name}: {record.getMessage()}"]
        parts += [f"{k}={json.dumps(v, default=str)}" for k, v in _fields(record).items()]
        line = " ".join(parts)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return redact(line)


def configure_logging(level: str | None = None, fmt: str | None = None) -> None:
    """Install one stderr handler on the root logger (idempotent across reloads)."""
    handler = logging.StreamHandler(sys.stderr)
    use_text = (fmt or settings.log_format).lower() == "text"
    handler.setFormatter(TextFormatter() if use_text else JsonFormatter())
    handler._trading_handler = True  # type: ignore[attr-defined]

    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not getattr(h, "_trading_handler", False)]
    root.addHandler(handler)
    root.setLevel((level or settings.log_level).upper())
    # Third-party clients are noisy at INFO and can echo request details.
    for noisy in ("httpx", "httpx2", "httpcore", "httpcore2", "urllib3", "alpaca", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields) -> None:
    """Log `event` with structured fields, e.g. log_event(log, "order.submitted", ticker="AAPL")."""
    logger.log(level, event, extra={k: v for k, v in fields.items() if k not in _RESERVED})
