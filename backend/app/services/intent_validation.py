"""Fail-closed checks on Claude's trade intents, applied per intent before the risk engine.

Pure and deterministic. An actionable intent (buy/sell) that fails any check is stored
with its violations and status `rejected_by_risk`, so it is visible but can never be
approved or auto-executed. watch/avoid intents are informational and only need to be
well-formed (the schema already guarantees that).

Checks:
- STALE_DATA: the market snapshot the model saw is older than MAX_DATA_AGE_SEC.
- STALE_QUOTE / INCOMPLETE_DATA: the symbol's data-quality flags (computed before the model
  saw the data) say its quote was stale while the market was open, or its price/history
  was missing.
- NO_MARKET_PRICE / INVALID_LIMIT_PRICE / LIMIT_PRICE_OFF_MARKET: the limit price must be
  positive and within MAX_LIMIT_DEVIATION_PCT of the last price the model was shown, so a
  fabricated or mistyped price can't become an order.
- INVALID_STOP_LOSS / INVALID_TAKE_PROFIT: both must be set and within sane bounds.
- MISSING_REASON / MISSING_RISKS / MISSING_EVIDENCE: a trade needs a stated thesis, its
  risks, and at least one cited input data point.
"""
MAX_STOP_LOSS_PCT = 50.0
MAX_TAKE_PROFIT_PCT = 500.0
MAX_EVIDENCE_ITEMS = 12
ACTIONABLE = ("buy", "sell")


def _v(code: str, message: str) -> dict:
    return {"code": code, "message": message}


def check_limit_price(limit_price, last_price: float | None, max_limit_deviation_pct: float) -> list[dict]:
    """The limit must be positive and within `max_limit_deviation_pct` of the last price.
    Shared by intent validation and the pre-submit entry guard in execution."""
    limit = float(limit_price or 0)
    if limit <= 0:
        return [_v("INVALID_LIMIT_PRICE", "limit_price must be greater than 0")]
    if not last_price or last_price <= 0:
        return [_v("NO_MARKET_PRICE", "no last price to verify the limit price against")]
    deviation = abs(limit / last_price - 1) * 100
    if deviation > max_limit_deviation_pct:
        return [_v("LIMIT_PRICE_OFF_MARKET",
                   f"limit {limit:.2f} is {deviation:.1f}% from last price "
                   f"{last_price:.2f} (max {max_limit_deviation_pct:.0f}%)")]
    return []


def validate_intent(intent: dict, *, last_price: float | None, data_age_sec: float,
                    max_data_age_sec: float, max_limit_deviation_pct: float,
                    quality: dict | None = None) -> list[dict]:
    """Return the list of violations (empty = passed). `intent` is a TradeIntent dict."""
    if intent.get("action") not in ACTIONABLE:
        return []

    violations: list[dict] = []
    if quality is None:
        violations.append(_v("NO_DATA_QUALITY", "no data-quality flags for this symbol"))
    if quality and quality.get("stale"):
        violations.append(_v("STALE_QUOTE", f"quote was {quality.get('quote_age_sec')}s old while the "
                                            "market was open"))
    if quality and quality.get("incomplete"):
        violations.append(_v("INCOMPLETE_DATA", f"missing {', '.join(quality.get('missing') or [])}"))
    if data_age_sec > max_data_age_sec:
        violations.append(_v("STALE_DATA", f"market data was {data_age_sec:.0f}s old at decision "
                                           f"time (max {max_data_age_sec:.0f}s)"))

    violations += check_limit_price(intent.get("limit_price"), last_price, max_limit_deviation_pct)

    stop = float(intent.get("stop_loss_percent") or 0)
    if not 0 < stop <= MAX_STOP_LOSS_PCT:
        violations.append(_v("INVALID_STOP_LOSS",
                             f"stop_loss_percent {stop} must be > 0 and <= {MAX_STOP_LOSS_PCT:.0f}"))
    take = float(intent.get("take_profit_percent") or 0)
    if not 0 < take <= MAX_TAKE_PROFIT_PCT:
        violations.append(_v("INVALID_TAKE_PROFIT",
                             f"take_profit_percent {take} must be > 0 and <= {MAX_TAKE_PROFIT_PCT:.0f}"))

    if not (intent.get("reason") or "").strip():
        violations.append(_v("MISSING_REASON", "a trade needs a stated thesis"))
    if not (intent.get("risks") or "").strip():
        violations.append(_v("MISSING_RISKS", "a trade needs its invalidation risks"))
    evidence = [e for e in (intent.get("evidence") or []) if isinstance(e, str) and e.strip()]
    if not evidence:
        violations.append(_v("MISSING_EVIDENCE", "a trade must cite at least one input data point"))
    elif len(evidence) > MAX_EVIDENCE_ITEMS:
        violations.append(_v("TOO_MUCH_EVIDENCE", f"cite at most {MAX_EVIDENCE_ITEMS} data points"))
    return violations
