"""Deterministic end-of-day (or any window) performance report built from the journal.

Reads only the database (trade outcomes, orders, fills, stored quotes/bars, candidate
outcomes); never calls the broker or Claude. `build_report` works on any [start, end)
window, so the weekly rollup reuses exactly the same code. Reports are stored in
`review_reports` (kind = daily|weekly; the human-review fields live in `data`)."""
import logging
import statistics
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.logging_config import log_event
from app.models import (
    AccountSnapshot,
    CandidateOutcome,
    IntentAction,
    IntentStatus,
    LatestQuote,
    MarketBar,
    Order,
    OrderFill,
    ReviewReport,
    TradeIntent,
    TradeOutcome,
)
from app.services.review import metrics as strat_metrics

logger = logging.getLogger("trading.review")

BENCHMARK = "SPY"
MISSED_LIMIT = 50


def day_bounds(d: date) -> tuple[datetime, datetime]:
    start = datetime.combine(d, time.min, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def week_bounds(d: date) -> tuple[datetime, datetime]:
    """The 7 UTC days ending with (and including) `d`."""
    _, end = day_bounds(d)
    return end - timedelta(days=7), end


def _f(v) -> float | None:
    return float(v) if v is not None else None


# --------------------------------------------------------------------------- #
# sections                                                                    #
# --------------------------------------------------------------------------- #
def _pnl_section(db: Session, start: datetime, end: datetime, errors: list[str]) -> dict:
    trades = strat_metrics.load_trade_records(db, start, end)
    stats = strat_metrics.trade_stats(trades)

    unrealized, missing = 0.0, []
    for o in db.execute(select(TradeOutcome).where(TradeOutcome.status == "open")).scalars():
        q = db.get(LatestQuote, o.symbol)
        last = None
        if q is not None:
            if q.last_trade_price:
                last = q.last_trade_price
            elif q.bid and q.ask:
                last = (q.bid + q.ask) / 2
        if last is None or o.entry_price is None:
            missing.append(o.symbol)
            continue
        unrealized += (last - float(o.entry_price)) * o.qty
    if missing:
        errors.append("unrealized P&L excludes positions without a stored quote/entry: "
                      f"{sorted(set(missing))}")

    orders = _orders_in(db, start, end)
    fees = 0.0
    for o in orders:
        raw = o.raw if isinstance(o.raw, dict) else {}
        try:
            fees += float(raw.get("fees") or raw.get("commission") or 0)
        except (TypeError, ValueError):
            pass

    return {
        "realized_pnl": stats["net_pnl"],
        "unrealized_pnl": round(unrealized, 2),
        "fees": round(fees, 2),
        "closed_trades": stats["sample_size"],
        "win_rate": stats["win_rate"],
        "avg_win": stats["avg_win"],
        "avg_loss": stats["avg_loss"],
        "expectancy": stats["expectancy"],
        "max_drawdown_pct": stats["max_drawdown_pct"],
        "net_return_pct": stats["net_return_pct"],
        "unrealized_note": "unrealized is marked at report time from stored quotes, not point-in-time",
    }


def _orders_in(db: Session, start: datetime, end: datetime) -> list[Order]:
    return list(db.execute(
        select(Order).where(Order.submitted_at >= start, Order.submitted_at < end).order_by(Order.id)
    ).scalars())


def _equity(db: Session, end: datetime) -> float | None:
    snap = db.execute(
        select(AccountSnapshot).where(AccountSnapshot.captured_at < end)
        .order_by(AccountSnapshot.captured_at.desc()).limit(1)
    ).scalar_one_or_none()
    return _f(snap.equity) if snap else None


def _slippage_and_turnover(db: Session, start: datetime, end: datetime) -> dict:
    """Slippage = fill vs limit, signed so positive is worse (buy fills above / sell below limit)."""
    orders = _orders_in(db, start, end)
    bps, dollars, turnover = [], 0.0, 0.0
    for o in orders:
        if not o.filled_qty or o.filled_avg_price is None:
            continue
        fill = float(o.filled_avg_price)
        turnover += fill * o.filled_qty
        if o.limit_price:
            limit = float(o.limit_price)
            sign = 1 if o.side.lower() == "buy" else -1
            diff = (fill - limit) * sign
            bps.append(diff / limit * 10_000)
            dollars += diff * o.filled_qty
    equity = _equity(db, end)
    return {
        "slippage_bps_avg": round(sum(bps) / len(bps), 2) if bps else None,
        "slippage_dollars": round(dollars, 2),
        "slippage_orders": len(bps),
        "turnover_notional": round(turnover, 2),
        "turnover_pct_equity": round(turnover / equity * 100, 2) if equity else None,
    }


def _broker_section(db: Session, start: datetime, end: datetime) -> dict:
    orders = _orders_in(db, start, end)
    counts = {"filled": 0, "rejected": 0, "canceled": 0, "expired": 0, "partially_filled": 0}
    latencies = []
    for o in orders:
        st = (o.status or "").lower()
        if st in ("cancelled",):
            st = "canceled"
        if st in counts:
            counts[st] += 1
        fills = db.execute(
            select(OrderFill).where(OrderFill.order_id == o.id, OrderFill.filled_qty > 0,
                                    OrderFill.filled_at.is_not(None)).order_by(OrderFill.filled_at)
        ).scalars().all()
        if fills and o.submitted_at:
            latencies.append(max(0.0, (fills[0].filled_at - o.submitted_at).total_seconds()))
    total = len(orders)
    return {
        "orders_submitted": total,
        **counts,
        "fill_rate": round(counts["filled"] / total, 4) if total else None,
        "submit_to_fill_seconds_avg": round(sum(latencies) / len(latencies), 2) if latencies else None,
        "submit_to_fill_seconds_median": round(statistics.median(latencies), 2) if latencies else None,
        "latency_samples": len(latencies),
    }


def _close_before(db: Session, symbol: str, ts: datetime) -> float | None:
    bar = db.execute(
        select(MarketBar).where(MarketBar.symbol == symbol, MarketBar.timeframe == "1Day", MarketBar.ts < ts)
        .order_by(MarketBar.ts.desc()).limit(1)
    ).scalar_one_or_none()
    return bar.close if bar else None


def _benchmark_section(db: Session, start: datetime, end: datetime, pnl: dict, errors: list[str]) -> dict:
    base, last = _close_before(db, BENCHMARK, start), _close_before(db, BENCHMARK, end)
    spy = round((last / base - 1) * 100, 4) if base and last else None
    equity = _equity(db, end)
    port = round(pnl["realized_pnl"] / equity * 100, 4) if equity else None
    if spy is None:
        errors.append(f"{BENCHMARK} bars missing for the period; benchmark comparison unavailable")
    return {
        "benchmark": BENCHMARK,
        "benchmark_return_pct": spy,
        "realized_return_pct_of_equity": port,
        "excess_return_pct": round(port - spy, 4) if port is not None and spy is not None else None,
    }


def _missed_ideas(db: Session, start: datetime, end: datetime) -> list[dict]:
    """Rejected/abstained (watch, avoid) ideas with their hypothetical forward outcomes."""
    intents = db.execute(
        select(TradeIntent).where(
            TradeIntent.created_at >= start, TradeIntent.created_at < end,
            (TradeIntent.status.in_([IntentStatus.rejected_by_risk, IntentStatus.rejected_by_user]))
            | (TradeIntent.action.in_([IntentAction.watch, IntentAction.avoid])),
        ).order_by(TradeIntent.id).limit(MISSED_LIMIT)
    ).scalars().all()
    out = []
    for i in intents:
        outs = db.execute(
            select(CandidateOutcome).where(CandidateOutcome.intent_id == i.id)
            .order_by(CandidateOutcome.horizon_days)
        ).scalars().all()
        out.append({
            "intent_id": i.id, "ticker": i.ticker, "action": i.action.value, "status": i.status.value,
            "strategy_id": i.strategy_id, "confidence": _f(i.confidence),
            "outcomes": [
                {"horizon_days": c.horizon_days, "return_pct": round(c.return_pct, 4),
                 "benchmark_return_pct": c.benchmark_return_pct,
                 "excess_return_pct": (round(c.return_pct - c.benchmark_return_pct, 4)
                                       if c.benchmark_return_pct is not None else None)}
                for c in outs
            ],
        })
    return out


# --------------------------------------------------------------------------- #
# report                                                                      #
# --------------------------------------------------------------------------- #
def build_report(db: Session, start: datetime, end: datetime) -> dict:
    """Pure read: all sections for [start, end). No writes."""
    errors: list[str] = []
    pnl = _pnl_section(db, start, end, errors)
    pnl.update({k: v for k, v in _slippage_and_turnover(db, start, end).items()})
    return {
        "pnl": pnl,
        "benchmark": _benchmark_section(db, start, end, pnl, errors),
        "broker": _broker_section(db, start, end),
        "missed_ideas": _missed_ideas(db, start, end),
        "errors": errors,
    }


def render_summary(kind: str, start: datetime, end: datetime, rep: dict) -> str:
    p, b, br = rep["pnl"], rep["benchmark"], rep["broker"]
    wr = f"{p['win_rate'] * 100:.0f}%" if p["win_rate"] is not None else "n/a"
    fr = f"{br['fill_rate'] * 100:.0f}%" if br["fill_rate"] is not None else "n/a"
    return (
        f"{kind} review {start.date()}..{(end - timedelta(seconds=1)).date()}: "
        f"{p['closed_trades']} closed trades, realized {p['realized_pnl']:+.2f}, unrealized "
        f"{p['unrealized_pnl']:+.2f}, win rate {wr}, expectancy {p['expectancy']}; "
        f"vs {b['benchmark']} {b['benchmark_return_pct']}%; "
        f"{br['orders_submitted']} orders, fill rate {fr}, rejected {br['rejected']}, "
        f"canceled {br['canceled']}, expired {br['expired']}; "
        f"{len(rep['missed_ideas'])} missed/abstained ideas."
    )


def store_report(db: Session, kind: str, start: datetime, end: datetime, rep: dict,
                 summary: str, *, strategy_rows: list[dict], llm_run_id: str | None = None,
                 suggested_changes: list | None = None,
                 extra_errors: list[str] | None = None) -> ReviewReport:
    """Replace any existing report of this kind/period (idempotent), keeping a recorded decision."""
    prior = db.execute(select(ReviewReport).where(
        ReviewReport.kind == kind, ReviewReport.period_start == start, ReviewReport.period_end == end
    )).scalars().all()
    decision = {}
    for p in prior:
        if (p.data or {}).get("approval_status", "pending") != "pending":
            decision = {k: p.data[k] for k in ("approval_status", "decided_by", "decided_at") if k in p.data}
    if prior:
        db.execute(delete(ReviewReport).where(ReviewReport.id.in_([p.id for p in prior])))
    data = {
        "period": kind,
        **{k: v for k, v in rep.items() if k != "errors"},
        "strategy_metrics": [
            {"strategy_id": r["strategy_id"], "strategy_version": r["strategy_version"], **r["metrics"]}
            for r in strategy_rows
        ],
        "errors": list(rep["errors"]) + list(extra_errors or []),
        "suggested_changes": suggested_changes or [],
        "approval_status": "pending",
        **decision,
    }
    row = ReviewReport(kind=kind, period_start=start, period_end=end, summary=summary, data=data,
                       llm_run_id=llm_run_id)
    db.add(row)
    db.flush()
    return row


def run_review(db: Session, d: date, *, weekly: bool = False, lookback_days: int = 90) -> list[ReviewReport]:
    """Generate (and store, replacing) the daily report for `d`, and the weekly one if asked."""
    from app.config import settings
    from app.services.review import summary as llm_summary

    reports = []
    for kind in ("daily", "weekly") if weekly else ("daily",):
        start, end = week_bounds(d) if kind == "weekly" else day_bounds(d)
        rep = build_report(db, start, end)
        # Strategy metrics: weekly uses its own window; daily uses a trailing window so samples accumulate.
        m_start = start if kind == "weekly" else end - timedelta(days=lookback_days)
        trades = strat_metrics.load_trade_records(db, m_start, end)
        rows = strat_metrics.store_strategy_metrics(db, kind, m_start, end, trades)
        summary = render_summary(kind, start, end, rep)
        suggested, llm_run_id, extra = [], None, []
        if settings.review_llm_summary:
            res = llm_summary.llm_review(rep, rows)
            if res["error"]:
                extra.append(res["error"])
            else:
                summary = f"{summary}\n\n{res['summary']}"
                suggested, llm_run_id = res["suggested_changes"], res["run_id"]
        reports.append(store_report(db, kind, start, end, rep, summary, strategy_rows=rows,
                                    llm_run_id=llm_run_id, suggested_changes=suggested,
                                    extra_errors=extra))
    db.commit()
    log_event(logger, "review_generated", date=str(d), weekly=weekly, report_ids=[r.id for r in reports])
    return reports
