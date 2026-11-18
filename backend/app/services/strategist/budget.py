"""Strategist spend ceilings, read straight from the `llm_calls` ledger (role "strategist").

Daily = since 00:00 UTC today, monthly = since the 1st of the UTC month. A call is allowed only
if spend so far plus a conservative estimate of the call stays within BOTH ceilings.
"""
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import LLMCall
from app.services import llm

ROLE = "strategist"


def spend_since(db: Session, since: datetime) -> float:
    total = db.scalar(select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0))
                      .where(LLMCall.role == ROLE, LLMCall.created_at >= since))
    return float(total or 0.0)


def estimate_call_cost(prompt_chars: int) -> float:
    """Upper-ish estimate: ~3 chars per input token plus the role's whole max_tokens as output."""
    r = llm.get_role(ROLE)
    return llm.estimate_cost(r.model, prompt_chars // 3 + 200, r.max_tokens) or 0.0


def check(db: Session, prompt_chars: int, now: datetime | None = None) -> tuple[bool, str | None, dict]:
    now = now or datetime.now(timezone.utc)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    daily, monthly = spend_since(db, day), spend_since(db, day.replace(day=1))
    projected = estimate_call_cost(prompt_chars)
    info = {"daily_spent_usd": round(daily, 6), "daily_limit_usd": settings.strategist_daily_budget_usd,
            "monthly_spent_usd": round(monthly, 6),
            "monthly_limit_usd": settings.strategist_monthly_budget_usd,
            "projected_call_usd": round(projected, 6)}
    if daily + projected > settings.strategist_daily_budget_usd:
        return False, "daily_budget", info
    if monthly + projected > settings.strategist_monthly_budget_usd:
        return False, "monthly_budget", info
    return True, None, info
