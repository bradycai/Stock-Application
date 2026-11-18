"""Recorded AI outputs, served back to the replay. Offline: never calls Claude.

Outputs already stored in `analyst_reports` / `strategy_proposals` (and AI trade intents) are
keyed by (role, model, prompt_version, ref_id), where ref_id is the bundle id (or intent id).
`RecordedOutputs` is a drop-in `call_fn` / `call_structured` stand-in for the strategist
pipeline: a hit returns the recorded output, a miss raises `CacheMiss` (fail closed). There is
deliberately no fall-through to `app.services.llm.call_structured` or the Anthropic client.
"""
from dataclasses import dataclass

from pydantic import BaseModel

from app.services.llm import LLMError, LLMResult

STRATEGIST = "strategist"
INTENT = "intent"


def analyst_role(name: str) -> str:
    return f"analyst:{name}"


@dataclass(frozen=True, order=True)
class RecordedKey:
    role: str
    model: str
    prompt_version: str
    ref_id: int

    def label(self) -> str:
        return f"{self.role}/{self.model}/{self.prompt_version}/{self.ref_id}"


class CacheMiss(LLMError):
    """No recorded output for the key. The replay skips the item; it never calls a model."""

    def __init__(self, key: RecordedKey):
        super().__init__("cache_miss", f"no recorded output for {key.label()}", {"key": key.label()})
        self.key = key


class RecordedOutputs:
    def __init__(self) -> None:
        self._items: dict[RecordedKey, dict] = {}
        self._bound_bundle: int | None = None
        self.hits = 0
        self.misses: list[RecordedKey] = []

    # -- building ---------------------------------------------------------------------------
    def add(self, role: str, model: str | None, prompt_version: str | None, ref_id: int,
            output: dict) -> RecordedKey:
        key = RecordedKey(role, model or "", prompt_version or "", int(ref_id))
        self._items[key] = output
        return key

    def __len__(self) -> int:
        return len(self._items)

    def keys(self) -> list[RecordedKey]:
        return sorted(self._items)

    # -- serving ----------------------------------------------------------------------------
    def get(self, role: str, model: str | None, prompt_version: str | None, ref_id: int) -> dict:
        key = RecordedKey(role, model or "", prompt_version or "", int(ref_id))
        if key not in self._items:
            self.misses.append(key)
            raise CacheMiss(key)
        self.hits += 1
        return self._items[key]

    def strategist_call_fn(self, *, model: str | None, prompt_version: str | None, bundle_id: int,
                           output_model: type[BaseModel]):
        """`call_fn(system, user)` for `strategist.pipeline.process_bundle`: the prompt text is
        ignored (it is data we are not sending anywhere); only the recorded output is served."""
        def call(system: str, user: str) -> LLMResult:
            raw = self.get(STRATEGIST, model, prompt_version, bundle_id)
            out = output_model.model_validate(raw)  # re-validated: recorded output is untrusted too
            return LLMResult(output=out, meta={"served_model": model, "prompt_version": prompt_version,
                                               "replayed": True})
        return call

    def bind(self, bundle_id: int | None) -> None:
        self._bound_bundle = bundle_id

    def call_structured(self, *, role, prompt_name, system, user, output_model, max_tokens=None,
                        model: str | None = None, prompt_version: str | None = None, **_ignored
                        ) -> LLMResult:
        """Stand-in for `llm.call_structured` (needs `bind(bundle_id)` first; model and
        prompt_version select the recorded variant)."""
        if self._bound_bundle is None:
            raise CacheMiss(RecordedKey(str(role), model or "", prompt_version or "", -1))
        name = str(prompt_name).removeprefix("research_")
        r = STRATEGIST if role == STRATEGIST else analyst_role(name)
        raw = self.get(r, model, prompt_version, self._bound_bundle)
        return LLMResult(output=output_model.model_validate(raw),
                         meta={"served_model": model, "prompt_version": prompt_version, "replayed": True})


def intent_output(it) -> dict:
    def f(x):
        return None if x is None else float(x)
    return {"ticker": it.ticker, "action": it.action.value, "limit_price": f(it.limit_price),
            "stop_loss_percent": f(it.stop_loss_percent), "take_profit_percent": f(it.take_profit_percent),
            "position_size_percent": f(it.position_size_percent), "confidence": f(it.confidence)}


def load_recorded(db, start=None, end=None) -> RecordedOutputs:
    """Build the cache from the database (read-only). `start`/`end` are datetimes on created_at."""
    from sqlalchemy import select

    from app.models import AnalysisRun, AnalystReportRow, StrategyProposal, TradeIntent

    rec = RecordedOutputs()

    def window(stmt, col):
        if start is not None:
            stmt = stmt.where(col >= start)
        if end is not None:
            stmt = stmt.where(col < end)
        return stmt

    for p in db.scalars(window(select(StrategyProposal).order_by(StrategyProposal.id),
                               StrategyProposal.created_at)):
        rec.add(STRATEGIST, p.model, p.strategy_version, p.bundle_id, p.proposal)
    for r in db.scalars(window(select(AnalystReportRow).order_by(AnalystReportRow.id),
                               AnalystReportRow.created_at)):
        if r.status == "ok" and r.report:
            rec.add(analyst_role(r.analyst), r.model, r.skill_version, r.bundle_id, r.report)
    rows = db.execute(window(
        select(TradeIntent, AnalysisRun.model).join(AnalysisRun, AnalysisRun.id == TradeIntent.run_id)
        .where(TradeIntent.strategy_id.is_not(None)).order_by(TradeIntent.id), TradeIntent.created_at))
    for it, model in rows:
        rec.add(INTENT, model, it.prompt_version, it.id, intent_output(it))
    return rec
