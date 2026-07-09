"""Autonomous-trading gating logic.

Two layers:
  * capability  — set by environment (AUTO_TRADING_ENABLED, plus the explicit live
                  confirmation when TRADING_MODE=live). This is the hard ceiling.
  * runtime     — the user's on/off switch, persisted in AutomationState, toggled via
                  the /automation endpoints.

Auto-execution requires BOTH, so leaving AUTO_TRADING_ENABLED=false (the default) keeps
the manual-approval flow completely unchanged.

The pure functions (`auto_capability`, `should_auto_execute`) take plain values so they
are trivially unit-testable without a DB, broker, or network.
"""
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
]


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


# --------------------------------------------------------------------------- #
# Runtime state (DB-backed)                                                   #
# --------------------------------------------------------------------------- #
def get_state(db: Session) -> AutomationState:
    state = db.get(AutomationState, 1)
    if state is None:
        state = AutomationState(id=1, enabled=False)
        db.add(state)
        db.flush()
    return state


def runtime_enabled(db: Session) -> bool:
    return get_state(db).enabled


def set_runtime(db: Session, enabled: bool, actor: str) -> AutomationState:
    state = get_state(db)
    state.enabled = enabled
    state.updated_at = datetime.now(timezone.utc)
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
    """Effective gate: capability AND runtime switch. Returns (active, reason)."""
    allowed, reason = auto_capability()
    if not allowed:
        return False, reason
    if not runtime_enabled(db):
        return False, "automation switch is OFF"
    return True, "active"
