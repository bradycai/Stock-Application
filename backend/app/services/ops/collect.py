"""DB readers that feed the pure checks (health.py) and the /ops/status snapshot.

Read-only; no broker, Claude or network access.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.logging_config import redact, safe_error
from app.models import (
    AuditLog, AutomationState, LatestQuote, LLMCall, MarketBar, NewsEvent, Order, ResearchJob, SystemAlert,
    WorkerHeartbeat,
)
from app.services import llm
from app.services.ops import health
from app.services.watcher import jobs

logger = logging.getLogger("trading.ops")

ROLES = ("strategist", "position", "research", "watcher")
# Order statuses that mean the broker refused or failed the order.
ORDER_FAILURE_STATUSES = ("rejected", "failed")


def expected_workers() -> list[str]:
    """Workers whose heartbeat must be fresh: only those enabled in settings."""
    out = []
    if settings.watcher_enabled:
        out.append("market_watcher")
    if settings.research_worker_enabled:
        out.append("research_worker")
    if settings.strategist_worker_enabled:
        out.append("strategist_worker")
    if settings.ops_expect_market_stream:
        out.append("market_stream")
    if getattr(settings, "scheduler_enabled", False):
        out.append("scheduler")
    return out


def role_budget(role: str) -> float | None:
    return {"strategist": settings.strategist_daily_budget_usd,
            "research": settings.research_daily_budget_usd,
            "watcher": settings.watcher_daily_budget_usd}.get(role)


def _age(ts: datetime | None, now: datetime) -> float | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return round((now - ts).total_seconds(), 1)


def heartbeats(db: Session) -> dict[str, WorkerHeartbeat]:
    return {h.worker: h for h in db.scalars(select(WorkerHeartbeat)).all()}


def data_freshness(db: Session, now: datetime) -> dict:
    quote = db.scalar(select(func.max(LatestQuote.quote_ts)))
    bar = db.scalar(select(func.max(MarketBar.ts)).where(MarketBar.timeframe == "1Min"))
    news = db.scalar(select(func.max(NewsEvent.published_at)))
    return {"latest_quote_age_sec": _age(quote, now), "latest_bar_age_sec": _age(bar, now),
            "latest_news_age_sec": _age(news, now), "market_open": health.market_open_approx(now)}


def llm_spend_today(db: Session, now: datetime) -> dict[str, float]:
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = dict(db.execute(select(LLMCall.role, func.coalesce(func.sum(LLMCall.cost_usd), 0.0))
                           .where(LLMCall.created_at >= day).group_by(LLMCall.role)).all())
    return {r: float(rows.get(r) or 0.0) for r in set(ROLES) | set(rows)}


def order_failures(db: Session, since: datetime) -> tuple[int, int]:
    """(order rows rejected/failed, submit errors audited as `order_error`) since `since`."""
    rejected = db.scalar(select(func.count()).select_from(Order).where(
        Order.status.in_(ORDER_FAILURE_STATUSES), Order.submitted_at >= since)) or 0
    errors = db.scalar(select(func.count()).select_from(AuditLog).where(
        AuditLog.event_type == "order_error", AuditLog.ts >= since)) or 0
    return int(rejected), int(errors)


def reconcile_mismatches(db: Session, since: datetime) -> dict[str, int]:
    rows = db.execute(select(AuditLog.event_type, func.count()).where(
        AuditLog.event_type.in_(health.RECONCILE_MISMATCH_EVENTS), AuditLog.ts >= since)
        .group_by(AuditLog.event_type)).all()
    return {e: int(n) for e, n in rows}


def failed_jobs_recent(db: Session, since: datetime) -> int:
    return int(db.scalar(select(func.count()).select_from(ResearchJob).where(
        ResearchJob.status == "failed", ResearchJob.updated_at >= since)) or 0)


def run_checks(db: Session, now: datetime) -> tuple[list[health.Finding], set[str]]:
    """Run every check independently. A check that raises yields a `health_check_error`
    warning and its kinds are NOT reported as ok (so nothing resolves on doubt).
    Returns (findings, kinds that were evaluated successfully)."""
    s = settings
    findings: list[health.Finding] = []
    ok_kinds: set[str] = set()

    def run(name: str, kinds: tuple[str, ...], fn) -> None:
        try:
            found = fn()
            db.rollback()  # end the read transaction; leaves nothing half-open
        except Exception as e:  # noqa: BLE001 - a failing check must never crash the loop
            db.rollback()
            logger.warning("health check %s failed: %s", name, safe_error(e))
            findings.append(health.check_error(name, safe_error(e)))
            return
        findings.extend(found)
        ok_kinds.update(kinds)

    def beats():
        return health.check_heartbeats({w: h.last_beat for w, h in heartbeats(db).items()},
                                       expected_workers(), now, s.ops_heartbeat_stale_sec)

    def market():
        f = data_freshness(db, now)
        return health.check_market_data(f["market_open"], f["latest_quote_age_sec"],
                                        f["latest_bar_age_sec"], s.ops_market_data_stale_sec)

    def orders_():
        rows, errors = order_failures(db, health.window_start(now, s.ops_order_failure_window_min))
        return health.check_order_failures(
            rows, errors, s.ops_order_failure_window_min, s.ops_order_failure_threshold)

    def budgets():
        spend = llm_spend_today(db, now)
        return health.check_llm_budgets({r: (v, role_budget(r)) for r, v in spend.items()},
                                        s.ops_budget_alert_pct)

    def research():
        return health.check_research_queue(jobs.counts(db), failed_jobs_recent(db, now - timedelta(hours=24)),
                                           s.ops_research_backlog_threshold, s.ops_research_failed_threshold)

    def reconcile():
        return health.check_reconcile(
            reconcile_mismatches(db, health.window_start(now, s.ops_reconcile_window_min)),
            s.ops_reconcile_window_min, s.ops_reconcile_mismatch_threshold)

    run("worker_heartbeats", ("worker_heartbeat_stale",), beats)
    run("market_data", ("market_data_stale",), market)
    run("order_failures", ("order_failures",), orders_)
    run("llm_budget", ("llm_budget",), budgets)
    run("research_queue", ("research_backlog", "research_jobs_failed"), research)
    run("reconcile", ("reconcile_mismatch",), reconcile)
    # check-error alerts clear when every check ran cleanly this cycle
    if not any(f.kind == "health_check_error" for f in findings):
        ok_kinds.add("health_check_error")
    return findings, ok_kinds


def recent_errors(db: Session, now: datetime, limit: int = 50) -> list[dict]:
    """Last 24h of failure-like audit events, redacted. `detail` is a bounded, redacted string."""
    since = now - timedelta(hours=24)
    rows = db.scalars(select(AuditLog).where(
        AuditLog.ts >= since,
        (AuditLog.event_type.in_(("order_error", "risk_rejection", "auto_order_blocked")
                                 + health.RECONCILE_MISMATCH_EVENTS))
        | AuditLog.event_type.like("%error%") | AuditLog.event_type.like("%failed%"))
        .order_by(AuditLog.id.desc()).limit(limit)).all()
    return [{"ts": r.ts.isoformat(), "event": redact(r.event_type),
             "detail": redact(str(r.payload or {}))[:300]} for r in rows]


def status_snapshot(db: Session, now: datetime) -> dict:
    beats = heartbeats(db)
    workers = []
    expected = set(expected_workers())
    for name in sorted(set(beats) | expected):
        h = beats.get(name)
        age = _age(h.last_beat, now) if h else None
        workers.append({
            "worker": name, "status": h.status if h else "missing",
            "last_beat": h.last_beat.isoformat() if h else None, "age_sec": age,
            "stale": bool(age is None or age > settings.ops_heartbeat_stale_sec),
            "details": _safe_details(h.details if h else {})})
    spend = llm_spend_today(db, now)
    roles = {}
    for r in ROLES:
        try:
            role = llm.get_role(r)
            model, effort = role.model, role.effort
        except Exception as e:  # noqa: BLE001 - bad config must not break the status page
            model, effort = f"invalid: {safe_error(e, 80)}", "unknown"
        roles[r] = {"model": model, "effort": effort, "spend_today_usd": round(spend.get(r, 0.0), 6),
                    "daily_budget_usd": role_budget(r)}
    active = {a: n for a, n in db.execute(
        select(SystemAlert.severity, func.count()).where(SystemAlert.status == "open")
        .group_by(SystemAlert.severity)).all()}
    state = db.get(AutomationState, 1)  # read-only: a missing row means manual / no kill switch
    return {
        "generated_at": now.isoformat(),
        "trading_mode": "live" if settings.is_live else "paper",
        "automation": {"mode": state.mode if state else "manual",
                       "kill_switch": bool(state.kill_switch) if state else False},
        "workers": workers,
        "queue": {"research_jobs": jobs.counts(db)},
        "data_freshness": data_freshness(db, now),
        "llm": {"spend_today_usd": round(sum(spend.values()), 6), "roles": roles},
        "alerts": {"open": int(sum(active.values())), "critical": int(active.get("critical", 0))},
        "recent_errors": recent_errors(db, now),
    }


def _safe_details(d: dict) -> dict:
    """Heartbeat details are worker-written JSON; run them through redaction defensively."""
    import json
    try:
        return json.loads(redact(json.dumps(d or {}, default=str)))
    except Exception:  # noqa: BLE001
        return {}
