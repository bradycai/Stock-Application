"""Read-only views of the market watcher: heartbeat, budget, decisions and the job queue."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import BigInteger, DateTime, String, column, select, true, values
from sqlalchemy.orm import Session

from app.config import settings
from app.deps import DB, Actor
from app.models import MarketBar, ResearchJob, SignalEvent, WatcherDecision
from app.services.watcher import budget, heartbeat, jobs

router = APIRouter(prefix="/watcher", tags=["watcher"])
WORKER = "market_watcher"


def _dt(t):
    return t.isoformat() if t else None


@router.get("/status")
def watcher_status(db: Session = DB, _actor: str = Actor):
    hb = heartbeat.get(db, WORKER)
    now = datetime.now(timezone.utc)
    beat = None
    if hb:
        age = (now - hb.last_beat).total_seconds()
        beat = {"status": hb.status, "started_at": _dt(hb.started_at), "last_beat": _dt(hb.last_beat),
                "age_seconds": round(age, 1),
                "stale": age > 3 * max(settings.watcher_interval_seconds,
                                       settings.watcher_closed_sleep_seconds),
                "details": hb.details}
    return {
        "enabled": settings.watcher_enabled,
        "model": settings.watcher_model,
        "interval_seconds": settings.watcher_interval_seconds,
        "heartbeat": beat,
        "last_cycle": (hb.details or {}).get("last_cycle") if hb else None,
        "budget": budget.status(db).as_dict(),
        "queue": jobs.counts(db),
    }


BAR_MAX_AGE = timedelta(days=1)   # an older bar is not "the price at decision time"
_PRICE_KEYS = ("price", "close")  # signal payload keys holding a traded/quoted price


def _utc(t):
    return t.replace(tzinfo=timezone.utc) if t is not None and t.tzinfo is None else t


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _signal_prices(db: Session, decisions) -> dict[int, dict]:
    """last_price / price_change_pct from the decision's own signal events (one query).

    Point in time: a signal counts only if it was created and occurred at or before the
    decision's created_at. LARGE_MOVE carries the move since the reference price."""
    ids = {i for d in decisions for i in (d.signal_event_ids or []) if isinstance(i, int)}
    if not ids:
        return {}
    by_id = {e.id: e for e in db.scalars(select(SignalEvent).where(SignalEvent.id.in_(ids)))}
    out: dict[int, dict] = {}
    for d in decisions:
        cutoff = _utc(d.created_at)
        events = [e for i in (d.signal_event_ids or []) if (e := by_id.get(i)) is not None
                  and e.symbol == d.symbol and _utc(e.created_at) <= cutoff and _utc(e.occurred_at) <= cutoff]
        events.sort(key=lambda e: (_utc(e.occurred_at), e.id), reverse=True)  # newest first
        res = {"last_price": None, "price_change_pct": None}
        for e in events:
            p = e.payload or {}
            if res["last_price"] is None:
                res["last_price"] = next((v for k in _PRICE_KEYS if (v := _num(p.get(k))) is not None), None)
            if res["price_change_pct"] is None:
                res["price_change_pct"] = _num(p.get("change_pct"))
        out[d.id] = res
    return out


def _bar_prices(db: Session, decisions) -> dict[int, float]:
    """Last completed 1Min bar close at or before each decision (one lateral query for all).

    A 1Min bar stamped T is complete at T+1min, so the cutoff is created_at - 1min."""
    rows = [(d.id, d.symbol, _utc(d.created_at) - timedelta(minutes=1)) for d in decisions]
    if not rows:
        return {}
    v = values(column("id", BigInteger), column("symbol", String), column("cutoff", DateTime(timezone=True)),
               name="d").data(rows)
    bar = (select(MarketBar.close).where(
        MarketBar.symbol == v.c.symbol, MarketBar.timeframe == "1Min",
        MarketBar.ts <= v.c.cutoff, MarketBar.ts >= v.c.cutoff - BAR_MAX_AGE)
        .order_by(MarketBar.ts.desc()).limit(1).lateral())
    q = select(v.c.id, bar.c.close).select_from(v.join(bar, true()))
    return {i: float(c) for i, c in db.execute(q).all() if c is not None}


@router.get("/decisions")
def watcher_decisions(limit: int = Query(50, ge=1, le=500), db: Session = DB, _actor: str = Actor):
    rows = db.scalars(select(WatcherDecision).order_by(WatcherDecision.id.desc()).limit(limit)).all()
    prices = _signal_prices(db, rows)
    missing = [d for d in rows if prices.get(d.id, {}).get("last_price") is None]
    bars = _bar_prices(db, missing)
    out = []
    for d in rows:
        p = dict(prices.get(d.id) or {"last_price": None, "price_change_pct": None})
        if p["last_price"] is None:
            p["last_price"] = bars.get(d.id)
        out.append({
            "id": d.id, "created_at": _dt(d.created_at), "symbol": d.symbol, "action": d.action,
            "event_type": d.event_type, "urgency": d.urgency, "evidence_ids": d.evidence_ids,
            "explanation": d.explanation, "expires_at": _dt(d.expires_at),
            "prompt_version": d.prompt_version, "model": d.model, "run_id": d.run_id,
            "signal_event_ids": d.signal_event_ids, "downgraded": d.downgraded,
            "last_price": p["last_price"], "price_change_pct": p["price_change_pct"],
        })
    return out


@router.get("/jobs")
def watcher_jobs(status: str | None = Query(None), limit: int = Query(100, ge=1, le=500),
                 db: Session = DB, _actor: str = Actor):
    if status is not None and status not in jobs.STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {list(jobs.STATUSES)}")
    q = select(ResearchJob).order_by(ResearchJob.id.desc()).limit(limit)
    if status:
        q = q.where(ResearchJob.status == status)
    return [{
        "id": j.id, "job_key": j.job_key, "kind": j.kind, "symbol": j.symbol, "status": j.status,
        "attempts": j.attempts, "max_attempts": j.max_attempts, "lease_until": _dt(j.lease_until),
        "leased_by": j.leased_by, "decision_id": j.decision_id, "signal_event_ids": j.signal_event_ids,
        "payload": j.payload, "last_error": j.last_error, "created_at": _dt(j.created_at),
        "updated_at": _dt(j.updated_at),
    } for j in db.scalars(q).all()]
