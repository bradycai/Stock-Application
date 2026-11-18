"""Hypothetical ("what if we had bought at the intent's date") outcomes for every intent,
including rejected and watch ones. Read-only over stored daily `market_bars`; never calls
the broker or any API. Rule "fwd-close-v1": start = first stored daily close on/after the
intent's UTC date, end = close N bars later (N = 1, 5, 20), with SPY over the same bars."""
import logging
from datetime import datetime, time, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.logging_config import log_event
from app.models import CandidateOutcome, MarketBar, TradeIntent

logger = logging.getLogger("trading.outcomes")

RULE_VERSION = "fwd-close-v1"
HORIZONS = (1, 5, 20)
BENCHMARK = "SPY"


def day_start(ts: datetime) -> datetime:
    ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    ts = ts.astimezone(timezone.utc)
    return datetime.combine(ts.date(), time.min, tzinfo=timezone.utc)


def daily_bars_from(db: Session, symbol: str, ts: datetime, limit: int) -> list[MarketBar]:
    """Up to `limit` stored daily bars (oldest first) on/after the UTC date of `ts`."""
    rows = db.execute(
        select(MarketBar)
        .where(MarketBar.symbol == symbol, MarketBar.timeframe == "1Day", MarketBar.ts >= day_start(ts))
        .order_by(MarketBar.ts, MarketBar.feed)
        .limit(limit * 2)  # headroom in case several feeds stored the same bar
    ).scalars().all()
    seen, out = set(), []
    for r in rows:  # one bar per timestamp
        if r.ts not in seen:
            seen.add(r.ts)
            out.append(r)
    return out[:limit]


def close_on_or_after(db: Session, symbol: str, ts: datetime) -> tuple[datetime, float] | None:
    bars = daily_bars_from(db, symbol, ts, 1)
    return (bars[0].ts, bars[0].close) if bars else None


def return_pct(start: float, end: float) -> float | None:
    if not start:
        return None
    return round((end / start - 1) * 100, 4)


def benchmark_return(db: Session, start_ts: datetime, end_ts: datetime) -> float | None:
    """SPY return between the first stored closes on/after the two timestamps."""
    a = close_on_or_after(db, BENCHMARK, start_ts)
    b = close_on_or_after(db, BENCHMARK, end_ts)
    if not a or not b:
        return None
    return return_pct(a[1], b[1])


def compute_candidate_outcomes(db: Session) -> dict:
    """Compute any missing (intent, horizon) outcomes that stored bars can support.
    Idempotent: existing rows (unique per intent/horizon/rule) are never recomputed."""
    max_h = max(HORIZONS)
    existing = {(i, h) for i, h in db.execute(
        select(CandidateOutcome.intent_id, CandidateOutcome.horizon_days)
        .where(CandidateOutcome.rule_version == RULE_VERSION)).all()}
    intents = db.execute(select(TradeIntent.id, TradeIntent.ticker, TradeIntent.created_at)
                         .order_by(TradeIntent.id)).all()
    written = skipped = 0
    for intent_id, ticker, created_at in intents:
        todo = [h for h in HORIZONS if (intent_id, h) not in existing]
        if not todo or created_at is None:
            continue
        bars = daily_bars_from(db, ticker, created_at, max_h + 1)
        spy = daily_bars_from(db, BENCHMARK, created_at, max_h + 1)
        for h in todo:
            if len(bars) <= h:
                skipped += 1
                continue
            start, end = bars[0], bars[h]
            ret = return_pct(start.close, end.close)
            if ret is None:
                skipped += 1
                continue
            bench = return_pct(spy[0].close, spy[h].close) if len(spy) > h else None
            db.execute(pg_insert(CandidateOutcome).values(
                intent_id=intent_id, horizon_days=h, rule_version=RULE_VERSION,
                start_ts=start.ts, end_ts=end.ts, start_price=start.close, end_price=end.close,
                return_pct=ret, benchmark_return_pct=bench,
            ).on_conflict_do_nothing(constraint="uq_candidate_outcomes_key"))
            written += 1
    db.commit()
    log_event(logger, "outcomes.candidates", written=written, skipped=skipped)
    return {"written": written, "skipped": skipped}
