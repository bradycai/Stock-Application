"""Pure health checks for the system health watcher: plain data in, `Finding`s out.

No DB, no network, no clock reads (callers pass `now`). Fail closed: an unknown age counts as
stale, and a check that itself errors becomes a warning finding (`check_error`).
"""
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

SEVERITIES = ("info", "warning", "critical")
_ET = ZoneInfo("America/New_York")

# Audit events written by app.services.reconcile when local state and the broker disagree.
RECONCILE_MISMATCH_EVENTS = (
    "reconcile_order_missing", "reconcile_unknown_position", "reconcile_order_lag",
    "reconcile_exit_not_filled",
)


@dataclass(frozen=True)
class Finding:
    kind: str
    key: str
    severity: str
    message: str
    details: dict = field(default_factory=dict)


def _age(ts: datetime | None, now: datetime) -> float | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds()


def market_open_approx(now: datetime) -> bool:
    """Regular US equity hours (Mon-Fri 09:30-16:00 ET). Ignores exchange holidays: the
    health watcher never calls the broker, so a holiday can only cause a stale-data warning."""
    et = now.astimezone(_ET)
    return et.weekday() < 5 and time(9, 30) <= et.time() < time(16, 0)


def check_error(name: str, error: str) -> Finding:
    return Finding("health_check_error", name, "warning", f"health check {name!r} failed: {error}",
                   {"check": name, "error": error})


def check_heartbeats(beats: dict[str, datetime | None], expected: list[str], now: datetime,
                     stale_sec: float) -> list[Finding]:
    """`beats` maps worker -> last_beat. Every expected worker must have a fresh beat; a
    missing worker or unknown age is stale. Workers that are not expected are ignored."""
    out = []
    for worker in expected:
        age = _age(beats.get(worker), now)
        if age is None or age > stale_sec or age < -stale_sec:
            out.append(Finding(
                "worker_heartbeat_stale", worker, "critical" if age is None else "warning",
                f"worker {worker} has no recent heartbeat"
                + ("" if age is None else f" (last {int(age)}s ago)"),
                {"worker": worker, "age_sec": age, "stale_after_sec": stale_sec}))
    return out


def check_market_data(market_open: bool | None, quote_age_sec: float | None, bar_age_sec: float | None,
                      stale_sec: float) -> list[Finding]:
    """Only while the market is open. Fresh if the newest of quote/bar is within `stale_sec`."""
    if market_open is not True:
        return []
    ages = [a for a in (quote_age_sec, bar_age_sec) if a is not None]
    if ages and min(ages) <= stale_sec:
        return []
    return [Finding("market_data_stale", "market", "warning",
                    "market data is stale while the market is open"
                    if ages else "no market data has been stored while the market is open",
                    {"latest_quote_age_sec": quote_age_sec, "latest_bar_age_sec": bar_age_sec,
                     "stale_after_sec": stale_sec})]


def check_order_failures(failures: int, window_min: int, threshold: int) -> list[Finding]:
    if failures < max(1, threshold):
        return []
    return [Finding("order_failures", "orders", "critical",
                    f"{failures} order rejects/failures in the last {window_min} min",
                    {"failures": failures, "window_min": window_min, "threshold": threshold})]


def check_llm_budgets(roles: dict[str, tuple[float, float | None]], pct: float) -> list[Finding]:
    """`roles` maps role -> (spend_today_usd, daily_budget_usd|None). Warn at `pct`% of the
    budget, critical at 100%. Roles without a (positive) budget are skipped."""
    out = []
    for role, (spend, budget) in sorted(roles.items()):
        if not budget or budget <= 0:
            continue
        used = spend / budget * 100
        if used >= 100:
            sev = "critical"
        elif used >= pct:
            sev = "warning"
        else:
            continue
        out.append(Finding("llm_budget", role, sev,
                           f"{role} LLM spend at {used:.0f}% of the daily budget "
                           f"(${spend:.2f} of ${budget:.2f})",
                           {"role": role, "spend_usd": round(spend, 4), "budget_usd": budget,
                            "pct": round(used, 1)}))
    return out


def check_research_queue(counts: dict[str, int], failed_recent: int, backlog_threshold: int,
                         failed_threshold: int) -> list[Finding]:
    out = []
    backlog = int(counts.get("queued", 0)) + int(counts.get("leased", 0))
    if backlog >= max(1, backlog_threshold):
        out.append(Finding("research_backlog", "research_jobs", "warning",
                           f"{backlog} research jobs are waiting",
                           {"queued": counts.get("queued", 0), "leased": counts.get("leased", 0),
                            "threshold": backlog_threshold}))
    if failed_recent >= max(1, failed_threshold):
        out.append(Finding("research_jobs_failed", "research_jobs", "warning",
                           f"{failed_recent} research jobs failed in the last 24h",
                           {"failed_24h": failed_recent, "threshold": failed_threshold}))
    return out


def check_reconcile(mismatches: dict[str, int], window_min: int, threshold: int) -> list[Finding]:
    """`mismatches` maps reconcile audit event -> count within the window."""
    total = sum(int(v) for v in mismatches.values())
    if total < max(1, threshold):
        return []
    return [Finding("reconcile_mismatch", "reconcile", "critical",
                    f"{total} reconciliation mismatches in the last {window_min} min",
                    {"total": total, "by_event": {k: v for k, v in mismatches.items() if v},
                     "window_min": window_min})]


def window_start(now: datetime, minutes: int) -> datetime:
    return now - timedelta(minutes=minutes)
