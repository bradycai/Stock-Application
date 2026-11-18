"""Evidence bundle builder: ONE frozen, timestamped bundle per leased research job.

Every fact the analysts and the strategist may cite becomes an `EvidenceItem` keyed by a
stable id (see contracts.py): "quote:AAPL", "ind:AAPL.rsi_14", "news:123", "fund:AAPL.revenue_annual",
"macro:DGS10", "regime:spy", "pos:AAPL", "signal:45", plus "decision:7" (the watcher decision that
caused the job), "quality:AAPL" and "filing:<accession>". Only facts that exist are added: a missing
value is a data gap (listed in `context["data_gaps"]`), never invented.

The market data comes from `data_aggregator.build_context` (read-only toward the broker) and is
saved as a replayable market snapshot linked from the bundle. Nothing here can place an order.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.logging_config import log_event
from app.models import Filing, ResearchBundle, ResearchJob, SignalEvent, WatcherDecision
from app.services import data_aggregator, snapshots
from app.services.research.contracts import EvidenceItem

logger = logging.getLogger("trading.research")

INSTITUTIONAL_FORMS = ("13F-HR", "13F-HR/A", "SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A",
                       "SCHEDULE 13D", "SCHEDULE 13G", "3", "4", "5")
_SKIP_IND = {"history_days", "data_sufficient", "last_bar_ts", "note"}


def _jsonable(value):
    return json.loads(json.dumps(value, default=str))


def _parse_ts(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


class _Evidence:
    def __init__(self) -> None:
        self.items: dict[str, dict] = {}

    def add(self, id: str, kind: str, value, as_of, source: str) -> None:
        item = EvidenceItem(id=id, kind=kind, value=_jsonable(value), as_of=_parse_ts(as_of), source=source)
        self.items[id] = item.model_dump(mode="json")


def _present(v) -> bool:
    return v is not None and not (isinstance(v, (list, dict)) and not v)


def build_evidence(symbol: str, entry: dict, ctx: dict, *, position: dict | None,
                   signals: list[SignalEvent], decision: WatcherDecision | None,
                   filings: list[Filing], now: datetime) -> tuple[dict[str, dict], list[str]]:
    """Turn one symbol's context into ({evidence id: EvidenceItem dict}, data gaps)."""
    ev = _Evidence()
    gaps: list[str] = []
    sym = symbol.upper()

    if entry.get("last_price"):
        ev.add(f"quote:{sym}", "quote",
               {k: entry.get(k) for k in ("last_price", "intraday_change_pct", "today_open", "volume")
                if entry.get(k) is not None},
               entry.get("last_trade_ts") or entry.get("quote_ts") or now, "alpaca")
    else:
        gaps.append("quote")

    ind = entry.get("indicators") or {}
    if ind.get("data_sufficient"):
        for k, v in ind.items():
            if k not in _SKIP_IND and _present(v):
                ev.add(f"ind:{sym}.{k}", "indicator", v, ind.get("last_bar_ts"), "daily_bars")
    else:
        gaps.append("indicators")

    news = entry.get("news") or []
    n_news = 0
    for n in news:
        nid = str(n.get("id") or "")
        if not nid.startswith("news:"):
            continue
        ev.add(nid, "news", {k: n.get(k) for k in ("headline", "summary", "publisher", "event_type",
                                                   "is_new") if n.get(k) is not None},
               n.get("published_at"), n.get("publisher") or "news")
        n_news += 1
    if not n_news:
        gaps.append("news")

    n_fund = 0
    for k, v in (entry.get("fundamentals") or {}).items():
        if _present(v):
            as_of = v.get("filed_at") if isinstance(v, dict) else None
            ev.add(f"fund:{sym}.{k}", "fundamental", v, as_of, "sec_edgar")
            n_fund += 1
    if not n_fund:
        gaps.append("fundamentals")

    series = (ctx.get("macro") or {}).get("series") or {}
    for sid, v in series.items():
        if v:
            ev.add(f"macro:{sid}", "macro", v, v.get("observation_date"), "fred")
    if not any(series.values()):
        gaps.append("macro")

    mc = ctx.get("market_context") or {}
    for etf, stats in (mc.get("indices") or {}).items():
        ev.add(f"regime:{etf.lower()}", "regime", stats, stats.get("as_of"), "market_context")
    if mc.get("regime_hint") and mc.get("regime_hint") != "unknown":
        ev.add("regime:summary", "regime",
               {k: mc.get(k) for k in ("regime_hint", "sector_breadth_pct_above_sma50", "leaders_20d",
                                       "laggards_20d", "volatility_proxy_spy_20d_pct",
                                       "classification_version")}, now, "market_context")
    if not mc.get("indices"):
        gaps.append("market_regime")

    if position:
        ev.add(f"pos:{sym}", "position", position, now, "broker")

    for s in signals:
        ev.add(f"signal:{s.id}", "signal", {"event_type": s.event_type, "source": s.source,
                                            "detail": s.payload}, s.occurred_at, "watcher")
    if decision is not None:
        ev.add(f"decision:{decision.id}", "other",
               {"action": decision.action, "urgency": decision.urgency, "event_type": decision.event_type,
                "explanation": decision.explanation}, decision.created_at, "watcher")

    if entry.get("quality"):
        ev.add(f"quality:{sym}", "other", entry["quality"], now, "snapshots")

    for f in filings:
        ev.add(f"filing:{f.accession}", "filing",
               {"form": f.form, "filed_at": f.filed_at, "period_of_report": f.period_of_report,
                "url": f.url}, f.filed_at, "sec_edgar")
    if not filings:
        gaps.append("institutional")
    return ev.items, gaps


def _exposure(positions: list[dict], equity: float, db: Session) -> tuple[list[dict], dict[str, float]]:
    rows, by_sector = [], {}
    for p in positions:
        sector = data_aggregator._sector_for(p["symbol"], db) or "unknown"
        weight = round(p.get("market_value", 0.0) / equity * 100, 2) if equity else None
        rows.append({"symbol": p["symbol"], "qty": p.get("qty"), "market_value": p.get("market_value"),
                     "weight_pct": weight, "sector": sector, "unrealized_plpc": p.get("unrealized_plpc")})
        if weight is not None:
            by_sector[sector] = round(by_sector.get(sector, 0.0) + weight, 2)
    return rows, by_sector


def build_bundle(db: Session, job: ResearchJob, *, now: datetime | None = None) -> ResearchBundle:
    """Create the `collecting` bundle for a leased job and flush it (the caller commits)."""
    now = now or datetime.now(timezone.utc)
    sym = job.symbol.upper()
    run_id = f"research-job-{job.id}"
    ctx, _account = data_aggregator.build_context([sym], db=db, run_id=run_id)
    snap = snapshots.save_snapshot(db, purpose="research", run_id=run_id, as_of=now, context=ctx)

    entry = (ctx.get("watchlist_data") or {}).get(sym) or {}
    held = next((p for p in ctx.get("positions") or [] if p.get("symbol", "").upper() == sym), None)
    signal_ids = [int(i) for i in (job.signal_event_ids or []) if str(i).lstrip("-").isdigit()]
    signals = (list(db.scalars(select(SignalEvent).where(SignalEvent.id.in_(signal_ids))))
               if signal_ids else [])
    decision = db.get(WatcherDecision, job.decision_id) if job.decision_id else None
    filings = list(db.scalars(select(Filing).where(Filing.symbol == sym, Filing.form.in_(INSTITUTIONAL_FORMS))
                              .order_by(Filing.filed_at.desc()).limit(10)))

    evidence, gaps = build_evidence(sym, entry, ctx, position=held, signals=signals, decision=decision,
                                    filings=filings, now=now)
    portfolio = ctx.get("portfolio") or {}
    equity = float(portfolio.get("equity") or 0)
    pos_rows, sector_exposure = _exposure(ctx.get("positions") or [], equity, db)
    market = ctx.get("market") or {}
    context = _jsonable({
        "portfolio": {k: portfolio.get(k) for k in ("equity", "cash", "buying_power", "day_pnl_pct",
                                                    "open_positions", "is_paper")},
        "positions": pos_rows,
        "sector_exposure_pct": sector_exposure,
        "symbol": {"symbol": sym, "sector": entry.get("sector"), "held": held is not None,
                   "weight_pct": next((r["weight_pct"] for r in pos_rows if r["symbol"] == sym), None)},
        "market": {"open": market.get("open"), "feed": market.get("feed")},
        "constraints": ctx.get("constraints"),
        "data_gaps": gaps,
        "job": {"id": job.id, "kind": job.kind, "payload": job.payload, "decision_id": job.decision_id,
                "signal_event_ids": signal_ids},
    })
    bundle = ResearchBundle(
        job_id=job.id, symbol=sym, kind=job.kind, status="collecting", as_of=now, holding=held is not None,
        market_snapshot_id=snap.id, evidence=evidence, context=context,
        expires_at=now + timedelta(minutes=settings.research_bundle_ttl_minutes))
    db.add(bundle)
    db.flush()
    log_event(logger, "research.bundle_built", bundle_id=bundle.id, symbol=sym, job_id=job.id,
              evidence=len(evidence), gaps=gaps)
    return bundle
