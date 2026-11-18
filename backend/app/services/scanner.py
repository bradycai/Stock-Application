"""Candidate scanner: ranks the static analysis universes OUTSIDE the watchlist and held
symbols with the deterministic prescreener, on stored bars and quotes only, and enqueues the
top few as INVESTIGATE research jobs. No Claude, no broker, no network, no orders: it can only
add rows to the research queue, capped per day and deduplicated per symbol per ET day."""
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.logging_config import log_event
from app.models import LatestQuote, MarketBar, PositionManagement, ResearchJob, Watchlist
from app.services import prescreener, universes
from app.services.indicators import technical_indicators
from app.services.scheduler.schedule import ET
from app.services.watcher import jobs

logger = logging.getLogger("trading.scanner")

BAR_LOOKBACK_DAYS = 400
KEY_PREFIX = "scan"


def universe_symbols() -> list[str]:
    wanted = [n.strip() for n in settings.scanner_universes.split(",") if n.strip()]
    names = wanted or list(universes.UNIVERSES)
    out: list[str] = []
    for n in names:
        out.extend(s.upper() for s in universes.get_universe(n))
    return list(dict.fromkeys(out))


def excluded_symbols(db: Session) -> set[str]:
    watch = db.scalars(select(Watchlist.ticker).where(Watchlist.active.is_(True))).all()
    held = db.scalars(select(PositionManagement.symbol).where(PositionManagement.is_open.is_(True))).all()
    return {s.upper() for s in [*watch, *held]}


def _market_data(db: Session, symbols: list[str], now: datetime) -> dict:
    """Prescreener input per symbol, built from stored daily bars and the latest quote."""
    since = now - timedelta(days=BAR_LOOKBACK_DAYS)
    bars: dict[str, list] = {s: [] for s in symbols}
    for b in db.scalars(select(MarketBar).where(
            MarketBar.symbol.in_(symbols), MarketBar.timeframe == "1Day", MarketBar.ts >= since,
            MarketBar.ts <= now).order_by(MarketBar.symbol, MarketBar.ts)):
        bars[b.symbol].append(SimpleNamespace(open=b.open, high=b.high, low=b.low, close=b.close,
                                              volume=b.volume, timestamp=b.ts))
    quotes = {q.symbol: q for q in db.scalars(select(LatestQuote).where(LatestQuote.symbol.in_(symbols)))}
    out = {}
    for s in symbols:
        ind = technical_indicators(bars[s]) if bars[s] else {"data_sufficient": False}
        q = quotes.get(s)
        out[s] = {"indicators": ind, "last_price": getattr(q, "last_trade_price", None) or 0.0,
                  "intraday_change_pct": 0.0, "news": []}
    return out


def jobs_enqueued_today(db: Session, day: str) -> int:
    return int(db.scalar(select(func.count()).select_from(ResearchJob)
                         .where(ResearchJob.job_key.like(f"{KEY_PREFIX}:{day}:%"))) or 0)


def scan(db: Session, now: datetime | None = None) -> dict:
    """One scan. Returns a summary dict; the caller commits. Never raises on missing data."""
    now = now or datetime.now(timezone.utc)
    day = now.astimezone(ET).date().isoformat()
    summary: dict = {"day": day, "enqueued": [], "candidates": 0}
    cap = settings.scanner_max_jobs_per_day
    remaining = cap - jobs_enqueued_today(db, day)
    summary["remaining_cap"] = max(0, remaining)
    if remaining <= 0:
        summary["skipped"] = "daily_cap_reached"
        return summary
    excl = excluded_symbols(db)
    symbols = [s for s in universe_symbols() if s not in excl]
    summary["candidates"] = len(symbols)
    if not symbols:
        summary["skipped"] = "empty_universe"
        return summary
    ranked = [r for r in prescreener.rank_candidates(_market_data(db, symbols, now), len(symbols))
              if r.score > -1e8]  # drop symbols without enough stored history
    limit = min(max(0, settings.scanner_top_n), remaining)
    for r in ranked:
        if len(summary["enqueued"]) >= limit:
            break
        job_id = jobs.enqueue(
            db, kind="INVESTIGATE", symbol=r.symbol, job_key=f"{KEY_PREFIX}:{day}:{r.symbol}",
            payload={"source": "scanner", "score": r.score, "components": r.components,
                     "scanned_at": now.isoformat()})
        if job_id is not None:  # None = already queued today: take the next symbol instead
            summary["enqueued"].append({"symbol": r.symbol, "score": r.score, "job_id": job_id})
    log_event(logger, "scanner.scan", day=day, enqueued=[e["symbol"] for e in summary["enqueued"]],
              candidates=len(symbols))
    return summary
