"""Submitted orders + live broker status refresh."""
from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import DB, Actor, audit
from app.models import IntentStatus, Order
from app.schemas import OrderOut
from app.services import broker

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("", response_model=list[OrderOut])
def list_orders(db: Session = DB, _actor: str = Actor):
    rows = db.scalars(select(Order).order_by(Order.submitted_at.desc()).limit(100)).all()
    return [OrderOut.model_validate(r) for r in rows]


@router.get("/{order_id}", response_model=OrderOut)
def get_order(order_id: int, db: Session = DB, _actor: str = Actor):
    """Fetch one order and refresh its status from the broker."""
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.broker_order_id and order.status not in ("filled", "canceled", "rejected"):
        try:
            status = broker.get_order_status(order.broker_order_id)
            order.status = status["status"]
            order.filled_qty = status["filled_qty"]
            order.filled_avg_price = status["filled_avg_price"]
            if order.status == "filled":
                if order.intent:
                    order.intent.status = IntentStatus.filled
                audit(db, "order_fill", "system", intent_id=order.intent_id,
                      order_id=order.id, payload=status)
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()

    return OrderOut.model_validate(order)
