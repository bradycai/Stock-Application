"""Automation gating: who may place orders without a human clicking "approve".

Two independent permissions must BOTH allow an automated order:
  * environment capability — AUTO_TRADING_ENABLED=true, plus
    AUTO_LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_THE_RISK when TRADING_MODE=live. This is
    the hard ceiling; it can't be changed from the UI.
  * runtime state (database, set via /automation) — mode == "auto" and the kill switch off.

Modes:
  manual  (default) nothing is automated; intents wait for manual approval.
  observe automation runs its decision logic and records what it WOULD do
          ("observe_would_execute" audit events) but places no orders.
  auto    eligible decisions execute, subject to the risk engine.

The kill switch persists until released. It blocks every automated order AND new buys
from manual approval; risk-reducing manual sells and broker-side stop orders still work.

`evaluate_gate` and `should_auto_execute` are pure and unit-tested; `order_gate(db)` is
the single check every automated order path calls immediately before touching the broker.
"""
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import settings
from app.models import AutomationState

# Audit event types that make up the automation event feed.
AUTOMATION_EVENTS = [
    "auto_buy_executed",
    "auto_sell_executed",
    "auto_trade_rejected",
    "stop_loss_triggered",
    "take_profit_triggered",
    "daily_loss_halt",
    "automation_enabled",
    "automation_disabled",
    "automation_mode_changed",
    "observe_would_execute",
    "auto_order_blocked",
    "kill_switch_engaged",
    "kill_switch_released",
]

MODES = ("manual", "observe", "auto")


def auto_capability(s=settings) -> tuple[bool, str]:
    """Environment capability gate. Returns (allowed, reason)."""
    if not s.auto_trading_enabled:
        return False, "AUTO_TRADING_ENABLED is false (manual approval only)"
    if s.is_live:
        # Three-key requirement for autonomous LIVE trading.
        if not s.live_auto_confirmed:
            return (
                False,
                "Autonomous LIVE trading requires "
                "AUTO_LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_THE_RISK",
            )
    return True, "ok"


def should_auto_execute(
    *,
    action: str,
    confidence_pct: float,
    stop_loss_pct: float,
    take_profit_pct: float,
    risk_passed: bool,
    min_confidence: float,
) -> tuple[bool, str, str]:
    """Per-intent eligibility for autonomous execution. Pure.

    Returns (ok, reason, category) where category is one of:
    'not_actionable' | 'risk_engine' | 'confidence' | 'missing_protection' | 'eligible'.
    The category lets the UI/logs say *why* a trade was auto-rejected.
    """
    if action not in ("buy", "sell"):
        return False, f"action '{action}' is not auto-executable", "not_actionable"
    if not risk_passed:
        return False, "risk engine did not pass the intent", "risk_engine"
    if confidence_pct < min_confidence:
        return (
            False,
            f"confidence {confidence_pct:.0f}% below threshold {min_confidence:.0f}%",
            "confidence",
        )
    if stop_loss_pct <= 0:
        return False, "stop loss required for auto-execution", "missing_protection"
    if take_profit_pct <= 0:
        return False, "take profit required for auto-execution", "missing_protection"
    return True, "eligible", "eligible"


@dataclass(frozen=True)
class OrderGate:
    """Whether an automated order may be placed right now, and why (not)."""
    allowed: bool
    observe: bool   # True: evaluate and record would-be orders, but place none
    mode: str
    kill_switch: bool
    reason: str


def evaluate_gate(*, mode: str, kill_switch: bool, capability_ok: bool,
                  capability_reason: str, kill_reason: str | None = None) -> OrderGate:
    """Pure decision table for automated orders. Fails closed on an unknown mode."""
    if kill_switch:
        return OrderGate(False, False, mode, True, f"kill switch engaged: {kill_reason or 'no reason given'}")
    if mode == "observe":
        return OrderGate(False, True, mode, False, "observe-only mode: decisions are logged, no orders")
    if mode != "auto":
        return OrderGate(False, False, mode, False,
                         "manual mode: automated orders disabled" if mode == "manual"
                         else f"unknown automation mode {mode!r}")
    if not capability_ok:
        return OrderGate(False, False, mode, False, capability_reason)
    return OrderGate(True, False, mode, False, "auto mode")


# --------------------------------------------------------------------------- #
# Runtime state (DB-backed)                                                   #
# --------------------------------------------------------------------------- #
def get_state(db: Session) -> AutomationState:
    state = db.get(AutomationState, 1)
    if state is None:
        state = AutomationState(id=1, mode="manual", kill_switch=False)
        db.add(state)
        db.flush()
    return state


def order_gate(db: Session) -> OrderGate:
    state = get_state(db)
    ok, reason = auto_capability()
    return evaluate_gate(mode=state.mode, kill_switch=state.kill_switch, capability_ok=ok,
                         capability_reason=reason, kill_reason=state.kill_switch_reason)


def set_mode(db: Session, mode: str, actor: str) -> AutomationState:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    state = get_state(db)
    state.mode = mode
    state.updated_at = datetime.now(timezone.utc)
    state.updated_by = actor
    db.flush()
    return state


def set_kill_switch(db: Session, engaged: bool, actor: str, reason: str | None = None) -> AutomationState:
    """Engaging also drops the mode to manual, so releasing never resumes automation by itself."""
    state = get_state(db)
    now = datetime.now(timezone.utc)
    state.kill_switch = engaged
    state.kill_switch_reason = (reason or "engaged from the dashboard") if engaged else None
    state.kill_switch_at = now if engaged else None
    state.kill_switch_by = actor if engaged else None
    if engaged:
        state.mode = "manual"
    state.updated_at = now
    state.updated_by = actor
    db.flush()
    return state


def clear_events(db: Session, actor: str) -> datetime:
    """Hide automation events older than now from the UI (audit_log itself is untouched)."""
    state = get_state(db)
    state.events_cleared_before = datetime.now(timezone.utc)
    state.updated_by = actor
    db.flush()
    return state.events_cleared_before


def auto_trading_active(db: Session) -> tuple[bool, str]:
    """Effective permission for automated orders. Returns (active, reason)."""
    gate = order_gate(db)
    return gate.allowed, gate.reason
