"""Replayable market snapshots and data-quality flags.

`quality_flags` runs on every symbol BEFORE any model sees the data:
  - stale: the market is open and the freshest quote/trade is older than MAX_QUOTE_AGE_SEC
    (or there is no timestamp at all);
  - incomplete: no usable last price or no price history to compute indicators from;
  - missing: every field that is unavailable (null), so it is labeled rather than guessed.
Actionable intents for a stale or incomplete symbol are rejected (intent_validation).

`save_snapshot` stores the exact context a run sent to the model plus its flags, feed and
market state, so the run can be replayed later; `store_market_data` persists the bars and
latest quotes that were fetched.
"""
import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import LatestQuote, MarketBar, MarketSnapshot
from app.services import market_data

INDICATOR_FIELDS = ("sma_20", "sma_50", "sma_200", "rsi_14", "macd", "atr_14", "relative_volume")
# PostgreSQL allows at most 65,535 bind parameters per statement; bars have 13 columns.
BAR_ROWS_PER_INSERT = 2000


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        ts = value
    else:
        try:
            ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def quality_flags(data: dict, *, market_open: bool | None, now: datetime, max_quote_age_sec: float) -> dict:
    """Pure: stale / incomplete / missing-field flags for one symbol's data."""
    missing: list[str] = []
    if not data.get("last_price"):
        missing.append("last_price")
    ind = data.get("indicators") or {}
    if not ind.get("data_sufficient"):
        missing.append("price_history")
    else:
        missing += [k for k in INDICATOR_FIELDS if ind.get(k) in (None, {})]
    fundamentals = data.get("fundamentals") or {}
    missing += [f"fundamentals.{k}" for k, v in fundamentals.items() if k != "missing" and v is None]

    stamps = [t for t in (_parse_ts(data.get("quote_ts")), _parse_ts(data.get("last_trade_ts"))) if t]
    age = (now - max(stamps)).total_seconds() if stamps else None
    stale = bool(market_open) and (age is None or age > max_quote_age_sec)
    return {
        "stale": stale,
        "incomplete": "last_price" in missing or "price_history" in missing,
        "missing": missing,
        "quote_age_sec": round(age, 1) if age is not None else None,
        "market_open": market_open,
    }


def content_hash(context: dict) -> str:
    return hashlib.sha256(json.dumps(context, sort_keys=True, default=str).encode()).hexdigest()


def save_snapshot(db: Session, *, purpose: str, run_id: str | None, as_of: datetime,
                  context: dict) -> MarketSnapshot:
    """Persist the exact context a run sent to the model, with its quality flags."""
    watch = context.get("watchlist_data") or {}
    market = context.get("market") or {}
    snap = MarketSnapshot(
        run_id=run_id, purpose=purpose, as_of=as_of, completed_at=datetime.now(timezone.utc),
        feed=market.get("feed") or market_data.feed_name(), market_open=market.get("open"),
        symbols=sorted(watch), data=json.loads(json.dumps(context, default=str)),
        quality={s: (d or {}).get("quality") for s, d in watch.items()},
        content_hash=content_hash(context),
    )
    db.add(snap)
    db.flush()
    return snap


def load_context(db: Session, snapshot_id: int) -> dict | None:
    """The stored context of a snapshot, exactly as the model saw it (for replay)."""
    snap = db.get(MarketSnapshot, snapshot_id)
    return snap.data if snap else None


def store_market_data(db: Session, bars_by_symbol: dict[str, list], quotes: dict[str, dict],
                      *, feed: str | None = None) -> dict:
    """Persist fetched daily bars (only those at or after each symbol's latest stored bar)
    and upsert latest quotes. Returns counts."""
    feed = feed or market_data.feed_name()
    latest = dict(db.execute(
        select(MarketBar.symbol, func.max(MarketBar.ts))
        .where(MarketBar.timeframe == "1Day", MarketBar.feed == feed,
               MarketBar.symbol.in_(list(bars_by_symbol)))
        .group_by(MarketBar.symbol)
    ).all()) if bars_by_symbol else {}

    rows = []
    for symbol, bars in bars_by_symbol.items():
        cutoff = latest.get(symbol)
        for bar in bars:
            if cutoff is None or bar.timestamp >= cutoff:
                rows.append({**market_data.bar_to_row(symbol, bar), "feed": feed,
                             "adjustment": "raw", "source": "alpaca"})
    for i in range(0, len(rows), BAR_ROWS_PER_INSERT):
        stmt = pg_insert(MarketBar).values(rows[i:i + BAR_ROWS_PER_INSERT])
        stmt = stmt.on_conflict_do_update(
            constraint="uq_market_bars_key",
            set_={c: stmt.excluded[c]
                  for c in ("open", "high", "low", "close", "volume", "vwap", "trade_count")},
        )
        db.execute(stmt)

    quote_rows = [{
        "symbol": s, "bid": q.get("bid"), "ask": q.get("ask"), "bid_size": q.get("bid_size"),
        "ask_size": q.get("ask_size"), "quote_ts": _parse_ts(q.get("quote_ts")),
        "last_trade_price": q.get("last_price") if q.get("price_source") == "last_trade" else None,
        "last_trade_ts": _parse_ts(q.get("last_trade_ts")), "feed": feed, "source": "snapshot",
    } for s, q in quotes.items()]
    if quote_rows:
        stmt = pg_insert(LatestQuote).values(quote_rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol"],
            set_={**{c: stmt.excluded[c] for c in ("bid", "ask", "bid_size", "ask_size", "quote_ts",
                                                    "last_trade_price", "last_trade_ts", "feed", "source")},
                  "updated_at": func.now()},
        )
        db.execute(stmt)
    db.flush()
    return {"bars_written": len(rows), "quotes_written": len(quote_rows)}
