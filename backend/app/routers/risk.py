"""Risk state + emergency kill switch + position-limit diagnostics."""
import logging
from datetime import date

from fastapi import APIRouter, HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.deps import DB, Actor, audit
from app.schemas import RiskStateOut
from app.services import broker, risk_state

logger = logging.getLogger("trading.risk")

router = APIRouter(prefix="/risk", tags=["risk"])


@router.get("/positions")
def position_diagnostics(_actor: str = Actor):
    """Explain EXACTLY why the position limit is (or isn't) triggered.

    Only FILLED positions count toward MAX_OPEN_POSITIONS. Pending, cancelled, rejected,
    and expired orders — and historical recommendations — never count.
    """
    try:
        positions = broker.get_positions()
        counts = broker.get_order_status_counts()
        pending_symbols = broker.get_open_order_symbols()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Broker error: {e}")

    filled = [{"symbol": p["symbol"], "qty": p["qty"]} for p in positions]
    filled_count = len(filled)
    limit = settings.max_open_positions
    limit_reached = filled_count >= limit

    logger.info(
        "POSITION DIAGNOSTIC: filled=%d (%s) pending=%d canceled=%d rejected=%d expired=%d "
        "max=%d limit_reached=%s",
        filled_count, [f["symbol"] for f in filled], counts["pending"], counts["canceled"],
        counts["rejected"], counts["expired"], limit, limit_reached,
    )

    return {
        "filled_positions": filled,
        "filled_count": filled_count,
        "max_open_positions": limit,
        "limit_reached": limit_reached,
        "would_block_new_symbol_buy": limit_reached,
        "explanation": (
            f"{filled_count} filled position(s) vs max {limit}. "
            + ("New positions in NEW tickers are blocked (TOO_MANY_POSITIONS) until you "
               "close one; adding to an already-held ticker is still allowed."
               if limit_reached else "Under the limit — new buys are allowed.")
        ),
        "order_counts": {
            "filled": counts["filled"],
            "pending": counts["pending"],
            "canceled": counts["canceled"],
            "rejected": counts["rejected"],
            "expired": counts["expired"],
            "by_status": counts["by_status"],
        },
        "pending_order_symbols": pending_symbols,
        "note": (
            "Only FILLED positions count toward the limit. Pending/cancelled/rejected/"
            "expired orders and historical recommendations do NOT count."
        ),
    }


def _state_out(db: Session, account: dict) -> RiskStateOut:
    state = risk_state.get_or_create_today(db, account["equity"])
    return RiskStateOut(
        trading_day=str(state.trading_day),
        start_equity=float(state.start_equity),
        current_equity=account["equity"],
        day_pnl_pct=round(account["day_pnl_pct"], 4),
        daily_loss_limit_pct=float(settings.daily_loss_limit_pct),
        trading_halted=state.trading_halted,
        halted_reason=state.halted_reason,
    )


@router.get("/state", response_model=RiskStateOut)
def get_state(db: Session = DB, _actor: str = Actor):
    try:
        account = broker.get_account()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Broker error: {e}")
    # is_halted also auto-halts if the daily loss limit is breached
    risk_state.is_halted(db, account)
    out = _state_out(db, account)
    db.commit()
    return out


@router.post("/halt", response_model=RiskStateOut)
def halt_trading(db: Session = DB, actor: str = Actor):
    """Emergency stop. Halts all approvals for the rest of the trading day."""
    try:
        account = broker.get_account()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Broker error: {e}")
    risk_state.halt(db, "Manual emergency stop", account)
    audit(db, "kill_switch", actor, payload={"action": "halt"})
    out = _state_out(db, account)
    db.commit()
    return out


@router.post("/resume", response_model=RiskStateOut)
def resume_trading(db: Session = DB, actor: str = Actor):
    try:
        account = broker.get_account()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Broker error: {e}")
    risk_state.resume(db, account)
    audit(db, "kill_switch", actor, payload={"action": "resume"})
    out = _state_out(db, account)
    db.commit()
    return out
