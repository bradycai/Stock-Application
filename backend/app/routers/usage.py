"""Read-only Claude usage / estimated cost views over the llm_calls ledger."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.deps import DB, Actor
from app.models import LLMCall

router = APIRouter(prefix="/llm", tags=["llm"])


@router.get("/usage")
def usage(days: int = Query(7, ge=1, le=90), db: Session = DB, _actor: str = Actor):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    day = func.date_trunc("day", func.timezone("UTC", LLMCall.created_at)).label("day")
    rows = db.execute(
        select(
            day, LLMCall.requested_model, LLMCall.role, LLMCall.status,
            func.count().label("calls"),
            func.sum(LLMCall.input_tokens), func.sum(LLMCall.output_tokens),
            func.sum(LLMCall.cache_read_tokens), func.sum(LLMCall.cache_write_tokens),
            func.sum(LLMCall.cost_usd),
        ).where(LLMCall.created_at >= since)
        .group_by(day, LLMCall.requested_model, LLMCall.role, LLMCall.status)
        .order_by(day.desc(), LLMCall.requested_model, LLMCall.role, LLMCall.status)
    ).all()
    groups = []
    totals = {"calls": 0, "failures": 0, "input_tokens": 0, "output_tokens": 0,
              "cache_read_tokens": 0, "cache_write_tokens": 0, "estimated_cost_usd": 0.0}
    for d, model, role, status, calls, i, o, cr, cw, cost in rows:
        g = {"day": d.date().isoformat(), "requested_model": model, "role": role, "status": status,
             "calls": calls, "failures": 0 if status == "ok" else calls,
             "input_tokens": int(i or 0), "output_tokens": int(o or 0),
             "cache_read_tokens": int(cr or 0), "cache_write_tokens": int(cw or 0),
             "estimated_cost_usd": round(float(cost or 0), 6)}
        groups.append(g)
        for k in totals:
            totals[k] += g[k]
    totals["estimated_cost_usd"] = round(totals["estimated_cost_usd"], 6)
    return {"days": days, "since": since, "estimated": True, "totals": totals, "groups": groups}


@router.get("/calls")
def calls(limit: int = Query(50, ge=1, le=200), db: Session = DB, _actor: str = Actor):
    rows = db.scalars(select(LLMCall).order_by(LLMCall.id.desc()).limit(limit)).all()
    return [{
        "id": c.id, "created_at": c.created_at, "run_id": c.run_id, "role": c.role,
        "prompt_name": c.prompt_name, "requested_model": c.requested_model,
        "served_model": c.served_model, "effort": c.effort, "status": c.status,
        "stop_reason": c.stop_reason, "fallback_used": c.fallback_used,
        "input_tokens": c.input_tokens, "output_tokens": c.output_tokens,
        "cache_read_tokens": c.cache_read_tokens, "cache_write_tokens": c.cache_write_tokens,
        "estimated_cost_usd": float(c.cost_usd) if c.cost_usd is not None else None,
        "elapsed_ms": c.elapsed_ms,
        "error": (c.error[:300] + "…") if c.error and len(c.error) > 300 else c.error,
    } for c in rows]
