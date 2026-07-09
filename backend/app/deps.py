"""Shared FastAPI dependencies: bearer-token auth and audit helper."""
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import AuditLog


def require_token(authorization: str = Header(default="")) -> str:
    """Simple shared-secret bearer auth. The frontend sends `Authorization: Bearer <API_TOKEN>`.

    Returns a stable actor id used in the audit log. Swap for real per-user JWT auth
    in production — every approval should be attributable to a specific human.
    """
    expected = f"Bearer {settings.api_token}"
    if not settings.api_token or authorization != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API token",
        )
    return "user:operator"


def audit(
    db: Session,
    event_type: str,
    actor: str,
    *,
    intent_id: int | None = None,
    order_id: int | None = None,
    payload: dict | None = None,
) -> None:
    """Write an append-only audit row. Caller is responsible for committing."""
    db.add(
        AuditLog(
            event_type=event_type,
            actor=actor,
            intent_id=intent_id,
            order_id=order_id,
            payload=payload or {},
        )
    )


DB = Depends(get_db)
Actor = Depends(require_token)
