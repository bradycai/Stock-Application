"""Watcher spend limits, from the `llm_calls` ledger (estimated list-price cost).

Daily = since 00:00 UTC today, monthly = since the 1st of the UTC month. A call is allowed
only if spend so far plus a conservative estimate of the call stays within BOTH ceilings.
"""
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import LLMCall
from app.services import llm


@dataclass
class BudgetStatus:
    daily_spent: float
    monthly_spent: float
    daily_limit: float
    monthly_limit: float

    def allows(self, projected_usd: float = 0.0) -> tuple[bool, str | None]:
        if self.daily_spent + projected_usd > self.daily_limit:
            return False, "daily_budget"
        if self.monthly_spent + projected_usd > self.monthly_limit:
            return False, "monthly_budget"
        return True, None

    def as_dict(self) -> dict:
        return {"daily_spent_usd": round(self.daily_spent, 6), "daily_limit_usd": self.daily_limit,
                "monthly_spent_usd": round(self.monthly_spent, 6), "monthly_limit_usd": self.monthly_limit}


def spend_since(db: Session, role: str, since: datetime) -> float:
    total = db.scalar(select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0))
                      .where(LLMCall.role == role, LLMCall.created_at >= since))
    return float(total or 0.0)


def status(db: Session, role: str = "watcher", now: datetime | None = None) -> BudgetStatus:
    now = now or datetime.now(timezone.utc)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return BudgetStatus(
        daily_spent=spend_since(db, role, day),
        monthly_spent=spend_since(db, role, day.replace(day=1)),
        daily_limit=settings.watcher_daily_budget_usd,
        monthly_limit=settings.watcher_monthly_budget_usd,
    )


def estimate_call_cost(prompt_chars: int, role: str = "watcher") -> float:
    """Upper-ish estimate: ~3 chars per input token, and the role's whole max_tokens as output."""
    r = llm.get_role(role)
    return llm.estimate_cost(r.model, prompt_chars // 3 + 200, r.max_tokens) or 0.0


def check(db: Session, prompt_chars: int, role: str = "watcher",
          now: datetime | None = None) -> tuple[bool, str | None, BudgetStatus]:
    st = status(db, role, now)
    ok, reason = st.allows(estimate_call_cost(prompt_chars, role))
    return ok, reason, st
