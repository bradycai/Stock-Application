"""Watchlist CRUD."""
from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import DB, Actor
from app.models import Watchlist
from app.schemas import WatchlistItem

router = APIRouter(prefix="/watchlist", tags=["watchlist"])


@router.get("", response_model=list[str])
def list_watchlist(db: Session = DB, _actor: str = Actor):
    rows = db.scalars(select(Watchlist).where(Watchlist.active.is_(True))).all()
    return [r.ticker for r in rows]


@router.post("", response_model=list[str])
def add_ticker(item: WatchlistItem, db: Session = DB, _actor: str = Actor):
    ticker = item.ticker.strip().upper()
    if not ticker:
        raise HTTPException(status_code=400, detail="Ticker required")
    existing = db.scalar(select(Watchlist).where(Watchlist.ticker == ticker))
    if existing:
        existing.active = True
    else:
        db.add(Watchlist(ticker=ticker))
    db.commit()
    rows = db.scalars(select(Watchlist).where(Watchlist.active.is_(True))).all()
    return [r.ticker for r in rows]


@router.delete("/{ticker}", response_model=list[str])
def remove_ticker(ticker: str, db: Session = DB, _actor: str = Actor):
    row = db.scalar(select(Watchlist).where(Watchlist.ticker == ticker.upper()))
    if row:
        row.active = False
        db.commit()
    rows = db.scalars(select(Watchlist).where(Watchlist.active.is_(True))).all()
    return [r.ticker for r in rows]
