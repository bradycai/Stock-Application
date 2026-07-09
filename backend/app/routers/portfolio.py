"""Portfolio, cash, and positions."""
from fastapi import APIRouter, HTTPException

from app.config import settings
from app.deps import Actor
from app.schemas import PortfolioOut
from app.services import broker

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


@router.get("", response_model=PortfolioOut)
def get_portfolio(_actor: str = Actor):
    try:
        account = broker.get_account()
        positions = broker.get_positions()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Broker error: {e}")

    return PortfolioOut(
        equity=account["equity"],
        cash=account["cash"],
        buying_power=account["buying_power"],
        day_pnl=account["day_pnl"],
        day_pnl_pct=round(account["day_pnl_pct"], 4),
        open_positions=len(positions),
        max_open_positions=settings.max_open_positions,
        is_paper=account["is_paper"],
        positions=positions,
    )
