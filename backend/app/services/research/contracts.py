"""Shared contract for the research pipeline (roadmap Phase 5).

Producer/consumer hand-offs, all through the database:

  research_jobs (watcher, PR 04)
     │  research worker (PR 05) leases a job and freezes ONE timestamped evidence bundle
     ▼
  research_bundles  status: collecting → ready → strategized | failed | expired
     │  specialist analysts (PR 05) each write one AnalystReport row against the bundle,
     │  citing only evidence ids from that bundle; then the worker marks the bundle `ready`
     ▼
  analyst_reports
     │  strategist worker (PR 06) leases `ready` bundles, sends Opus the reports + evidence +
     │  portfolio exposure + strategy history, validates the StrategistProposal, stores it,
     │  and (unless it abstains or fails validation) converts it into a pending trade intent
     ▼
  strategy_proposals → trade_intents (strategy_id "opus-strategist") → existing risk /
  approval / automation flow. Nothing in the research pipeline ever places an order.

Evidence ids are stable strings scoped to a bundle: "quote:AAPL", "ind:AAPL.rsi_14",
"news:123", "fund:AAPL.revenue", "macro:DGS10", "regime:spy", "pos:AAPL", "signal:45".
A report or proposal citing an id that is not in its bundle is rejected (fail closed).
"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ANALYSTS = ("technical", "news", "fundamental", "market_regime", "institutional")
BUNDLE_STATUSES = ("collecting", "ready", "strategized", "failed", "expired")
PROPOSAL_STATUSES = ("proposed", "converted", "rejected", "abstained", "expired")

# allow_inf_nan=False: Infinity/NaN are never valid prices or scores, and PostgreSQL JSONB can't
# store them — a model emitting one must fail validation, not crash the write later.
_STRICT = ConfigDict(extra="forbid", allow_inf_nan=False)


class EvidenceItem(BaseModel):
    """One fact in a bundle: what it is, its value, when it was true, and where it came from."""
    model_config = _STRICT

    id: str
    kind: Literal["quote", "indicator", "news", "fundamental", "macro", "regime", "position",
                  "signal", "filing", "other"]
    value: str | float | int | bool | dict | list | None
    as_of: datetime | None
    source: str


class AnalystReport(BaseModel):
    """Structured output every specialist analyst returns (stored in analyst_reports)."""
    model_config = _STRICT

    symbol: str
    analyst: Literal["technical", "news", "fundamental", "market_regime", "institutional"]
    stance: Literal["bullish", "bearish", "neutral", "insufficient_data"]
    confidence: float = Field(ge=0, le=1)
    summary: str
    key_points: list[str]
    evidence_ids: list[str]
    risks: list[str]
    invalidation: str
    data_gaps: list[str]


class StrategistProposal(BaseModel):
    """Structured output of the Opus strategist (stored in strategy_proposals).
    `action` HOLD/WATCH/AVOID or a non-empty `abstain_reason` never becomes a trade."""
    model_config = _STRICT

    symbol: str
    action: Literal["BUY", "SELL", "HOLD", "WATCH", "AVOID"]
    confidence: float = Field(ge=0, le=1)
    thesis: str
    entry_low: float = Field(ge=0)
    entry_high: float = Field(ge=0)
    invalidation_price: float = Field(ge=0)
    take_profit_price: float = Field(ge=0)
    position_size_percent: float = Field(ge=0, le=100)
    holding_horizon_days: int = Field(ge=0, le=365)
    exit_conditions: list[str]
    evidence_ids: list[str]
    conflicting_evidence: list[str]
    risks: list[str]
    limitations: list[str]
    abstain_reason: str
