"""Helpers for the daily risk state / kill switch."""
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.config import settings
from app.models import RiskState


def get_or_create_today(db: Session, current_equity: float) -> RiskState:
    today = date.today()
    state = db.get(RiskState, today)
    if state is None:
        state = RiskState(trading_day=today, start_equity=current_equity)
        db.add(state)
        db.flush()
    return state


def is_halted(db: Session, account: dict) -> tuple[bool, str | None]:
    """Returns (halted, reason). Halts on a manual flag OR when the day's P&L has hit
    the daily loss limit."""
    state = get_or_create_today(db, account["equity"])

    if state.trading_halted:
        return True, state.halted_reason or "Trading manually halted"

    day_pnl_pct = Decimal(str(account["day_pnl_pct"]))
    if day_pnl_pct <= -settings.daily_loss_limit_pct:
        # Auto-halt and persist
        state.trading_halted = True
        state.halted_reason = (
            f"Daily loss limit hit: {day_pnl_pct}% <= -{settings.daily_loss_limit_pct}%"
        )
        state.halted_at = datetime.now(timezone.utc)
        db.flush()
        return True, state.halted_reason

    return False, None


def halt(db: Session, reason: str, account: dict) -> RiskState:
    state = get_or_create_today(db, account["equity"])
    state.trading_halted = True
    state.halted_reason = reason
    state.halted_at = datetime.now(timezone.utc)
    db.flush()
    return state


def resume(db: Session, account: dict) -> RiskState:
    state = get_or_create_today(db, account["equity"])
    state.trading_halted = False
    state.halted_reason = None
    state.halted_at = None
    db.flush()
    return state
