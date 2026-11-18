"""Strategy metrics from closed trades, grouped by strategy/version and by regime, holding
period, news event type and confidence bucket. Pure stats helpers plus a DB loader/writer.
Groups below the minimum sample size are stored but flagged `sufficient: false`; nothing may
recommend a change from them."""
import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import MarketSnapshot, NewsEvent, StrategyMetric, TradeIntent, TradeOutcome

_NEWS_RE = re.compile(r"^news:(\d+)$")


@dataclass
class TradeRecord:
    pnl: float
    ret_pct: float
    exit_ts: datetime
    strategy_id: str
    strategy_version: str | None
    regime: str = "unknown"
    holding_days: float | None = None
    news_event_type: str | None = None
    confidence: float | None = None


# --------------------------------------------------------------------------- #
# pure helpers                                                                #
# --------------------------------------------------------------------------- #
def max_drawdown_pct(returns_pct: list[float]) -> float:
    """Largest peak-to-trough fall (percent, >= 0) of the compounded curve of sequential returns."""
    value = peak = 1.0
    worst = 0.0
    for r in returns_pct:
        value *= 1 + r / 100.0
        peak = max(peak, value)
        worst = max(worst, (peak - value) / peak)
    return worst * 100.0


def trade_stats(trades: list[TradeRecord], min_sample: int | None = None) -> dict:
    min_sample = settings.review_min_sample_size if min_sample is None else min_sample
    ordered = sorted(trades, key=lambda t: t.exit_ts)
    n = len(ordered)
    wins = [t for t in ordered if t.pnl > 0]
    losses = [t for t in ordered if t.pnl < 0]
    rets = [t.ret_pct for t in ordered]
    curve = 1.0
    for r in rets:
        curve *= 1 + r / 100.0
    return {
        "sample_size": n,
        "sufficient": n >= min_sample,
        "min_sample_size": min_sample,
        "net_pnl": round(sum(t.pnl for t in ordered), 2),
        "net_return_pct": round((curve - 1) * 100, 4) if n else 0.0,
        "win_rate": round(len(wins) / n, 4) if n else None,
        "avg_win": round(sum(t.pnl for t in wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(t.pnl for t in losses) / len(losses), 2) if losses else None,
        "expectancy": round(sum(t.pnl for t in ordered) / n, 2) if n else None,
        "expectancy_pct": round(sum(rets) / n, 4) if n else None,
        "max_drawdown_pct": round(max_drawdown_pct(rets), 4),
    }


def confidence_edges() -> list[float]:
    return sorted({float(x) for x in settings.review_confidence_buckets.split(",") if x.strip()})


def confidence_bucket(conf: float | None, edges: list[float] | None = None) -> str:
    if conf is None:
        return "unknown"
    edges = confidence_edges() if edges is None else edges
    lower, upper = None, None
    for e in edges:
        if conf >= e:
            lower = e
        else:
            upper = e
            break
    if lower is None:
        return f"<{edges[0]:g}" if edges else "all"
    return f">={lower:g}" if upper is None else f"{lower:g}-{upper:g}"


def holding_bucket(days: float | None) -> str:
    if days is None:
        return "unknown"
    if days < 1:
        return "intraday"
    if days <= 5:
        return "1-5d"
    if days <= 20:
        return "5-20d"
    return ">20d"


def calibration(trades: list[TradeRecord], min_sample: int | None = None) -> list[dict]:
    """Per confidence bucket: mean predicted confidence vs realized hit rate (pnl > 0)."""
    min_sample = settings.review_min_sample_size if min_sample is None else min_sample
    edges = confidence_edges()
    buckets: dict[str, list[TradeRecord]] = {}
    for t in trades:
        if t.confidence is not None:
            buckets.setdefault(confidence_bucket(t.confidence, edges), []).append(t)
    out = []
    for key, ts in sorted(buckets.items()):
        pred = sum(t.confidence for t in ts) / len(ts)
        hit = sum(1 for t in ts if t.pnl > 0) / len(ts)
        out.append({"bucket": key, "sample_size": len(ts), "sufficient": len(ts) >= min_sample,
                    "predicted_confidence": round(pred, 4), "realized_hit_rate": round(hit, 4),
                    "gap": round(pred - hit, 4)})
    return out


def group_trades(trades: list[TradeRecord]) -> dict[tuple, list[TradeRecord]]:
    """Key = (strategy_id, strategy_version, dimension, value). Every trade lands in an 'all' group."""
    edges = confidence_edges()
    groups: dict[tuple, list[TradeRecord]] = {}
    for t in trades:
        base = (t.strategy_id, t.strategy_version)
        dims = [("all", "all"), ("regime", t.regime or "unknown"),
                ("holding_period", holding_bucket(t.holding_days)),
                ("confidence", confidence_bucket(t.confidence, edges))]
        if t.news_event_type:
            dims.append(("news_event_type", t.news_event_type))
        for dim, val in dims:
            groups.setdefault((*base, dim, val), []).append(t)
    return groups


def compute_group_metrics(trades: list[TradeRecord], min_sample: int | None = None) -> list[dict]:
    rows = []
    items = sorted(group_trades(trades).items(), key=lambda kv: tuple(map(str, kv[0])))
    for (sid, ver, dim, val), ts in items:
        m = trade_stats(ts, min_sample)
        m["group"] = {"dimension": dim, "value": val}
        if dim == "all":
            m["calibration"] = calibration(ts, min_sample)
        rows.append({"strategy_id": sid, "strategy_version": ver, "metrics": m})
    return rows


# --------------------------------------------------------------------------- #
# DB                                                                          #
# --------------------------------------------------------------------------- #
def _regime_for(db: Session, intent: TradeIntent) -> str:
    """Regime recorded in the market snapshot the intent was decided on (point-in-time)."""
    sid = intent.market_snapshot_id
    if sid is None and intent.run is not None:
        sid = getattr(intent.run, "market_snapshot_id", None)
    if sid is None:
        return "unknown"
    snap = db.get(MarketSnapshot, sid)
    try:
        return str(snap.data["market_context"]["regime_hint"]) or "unknown"
    except (AttributeError, KeyError, TypeError):
        return "unknown"


def _news_type_for(db: Session, evidence: list | None) -> str | None:
    for item in evidence or []:
        m = _NEWS_RE.match(str(item).strip())
        if m:
            ev = db.get(NewsEvent, int(m.group(1)))
            if ev is not None:
                return ev.event_type
    return None


def load_trade_records(db: Session, start: datetime, end: datetime) -> list[TradeRecord]:
    """Closed trades whose exit happened in [start, end)."""
    rows = db.execute(
        select(TradeOutcome, TradeIntent)
        .join(TradeIntent, TradeIntent.id == TradeOutcome.intent_id)
        .where(TradeOutcome.status == "closed", TradeOutcome.realized_pnl.is_not(None),
               TradeOutcome.exit_ts >= start, TradeOutcome.exit_ts < end)
        .order_by(TradeOutcome.exit_ts, TradeOutcome.id)
    ).all()
    out = []
    for o, i in rows:
        out.append(TradeRecord(
            pnl=float(o.realized_pnl),
            ret_pct=float(o.realized_pnl_pct) if o.realized_pnl_pct is not None else 0.0,
            exit_ts=o.exit_ts,
            strategy_id=i.strategy_id or "unknown",
            strategy_version=i.strategy_version,
            regime=_regime_for(db, i),
            holding_days=o.holding_days,
            news_event_type=_news_type_for(db, i.evidence),
            confidence=float(i.confidence) if i.confidence is not None else None,
        ))
    return out


def store_strategy_metrics(db: Session, period: str, start: datetime, end: datetime,
                           trades: list[TradeRecord]) -> list[dict]:
    """Replace this period's strategy_metrics rows (idempotent). Caller commits."""
    db.execute(delete(StrategyMetric).where(
        StrategyMetric.window_start == start, StrategyMetric.window_end == end,
        StrategyMetric.metrics["period"].astext == period))
    rows = compute_group_metrics(trades)
    for r in rows:
        r["metrics"]["period"] = period
        db.add(StrategyMetric(strategy_id=r["strategy_id"], strategy_version=r["strategy_version"],
                              window_start=start, window_end=end, metrics=r["metrics"]))
    db.flush()
    return rows
