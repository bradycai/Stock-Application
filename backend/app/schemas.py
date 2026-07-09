"""Pydantic schemas: the Claude structured-output contract plus API request/response
models."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Claude structured-output contract                                           #
# --------------------------------------------------------------------------- #
class TradeIntent(BaseModel):
    """Exactly the JSON Claude is allowed to return per intent.

    Note: there is NO absolute-quantity field. Claude proposes a percentage of the
    portfolio; the backend computes share counts from live equity and price. The
    `le=...` bounds mirror the risk rules so Claude's output is self-consistent, but
    the RISK ENGINE is the enforcement — not this schema.
    """
    ticker: str
    action: Literal["buy", "sell", "watch", "avoid"]
    order_type: Literal["limit"] = "limit"
    limit_price: float = Field(ge=0)
    # No upper bound here on purpose: if Claude over-proposes size, we do NOT want the
    # whole run to fail client-side validation. The risk engine rejects an oversize
    # intent (POSITION_TOO_LARGE) and records it — that is the authoritative guard.
    position_size_percent: float = Field(ge=0)
    stop_loss_percent: float = Field(ge=0)
    take_profit_percent: float = Field(ge=0)
    confidence: float = Field(ge=0, le=1)
    reason: str
    risks: str


class AnalysisOutput(BaseModel):
    intents: list[TradeIntent]


class PositionDecision(BaseModel):
    """Claude's re-evaluation of a HELD position ('would we buy it again right now?')."""
    ticker: str
    action: Literal["hold", "trim", "sell", "add", "tighten_stop"]
    confidence: float = Field(ge=0, le=1)
    reason: str
    updated_thesis: str = ""
    exit_condition: str = ""


class MonitorDecision(BaseModel):
    """Claude's decision for a single triggered ticker during continuous monitoring."""
    ticker: str
    action: Literal["hold", "buy", "sell", "trim", "add", "tighten_stop", "raise_take_profit"]
    confidence: float = Field(ge=0, le=1)
    reason: str
    size_percent: float = Field(default=0, ge=0)            # for buy/add
    stop_loss_percent: float = Field(default=0, ge=0)        # for buy/add/tighten_stop
    take_profit_percent: float = Field(default=0, ge=0)      # for buy/add/raise_take_profit
    limit_price: float = Field(default=0, ge=0)              # for buy/add


# --------------------------------------------------------------------------- #
# API responses                                                               #
# --------------------------------------------------------------------------- #
class PositionOut(BaseModel):
    symbol: str
    qty: float
    avg_entry_price: float
    current_price: float
    market_value: float
    unrealized_pl: float
    unrealized_plpc: float


class PortfolioOut(BaseModel):
    equity: float
    cash: float
    buying_power: float
    day_pnl: float
    day_pnl_pct: float
    open_positions: int
    max_open_positions: int
    is_paper: bool
    positions: list[PositionOut]


class WatchlistItem(BaseModel):
    ticker: str


class ViolationOut(BaseModel):
    code: str
    message: str


class IntentOut(BaseModel):
    id: int
    run_id: int
    created_at: datetime
    ticker: str
    action: str
    order_type: str
    limit_price: float | None
    position_size_percent: float | None
    stop_loss_percent: float | None
    take_profit_percent: float | None
    confidence: float | None
    reason: str | None
    risks: str | None
    status: str
    risk_passed: bool | None
    risk_violations: list[ViolationOut] | None
    computed_qty: int | None
    computed_notional: float | None
    is_live: bool
    auto_executed: bool

    class Config:
        from_attributes = True


class OrderOut(BaseModel):
    id: int
    intent_id: int
    broker_order_id: str | None
    symbol: str
    side: str
    qty: int
    order_type: str
    limit_price: float | None
    stop_loss_price: float | None
    take_profit_price: float | None
    status: str
    filled_qty: int
    filled_avg_price: float | None
    is_live: bool
    submitted_at: datetime
    exit_reason: str | None = None
    monitoring_status: str | None = None

    class Config:
        from_attributes = True


class RunAnalysisRequest(BaseModel):
    tickers: list[str] | None = None  # defaults to the active watchlist
    universe: str | None = None       # optional predefined universe name (overrides watchlist)


class RunSummaryOut(BaseModel):
    tickers_screened: int
    prescreen_top_n: int          # how many candidates were sent to Claude
    batches: int
    failed_batches: int
    final_recommendations: int
    universe: str | None = None
    intents: list[IntentOut]


class RejectRequest(BaseModel):
    reason: str = "Rejected by user"


class AutomationStatusOut(BaseModel):
    capability_allowed: bool       # env permits automation at all
    capability_reason: str
    runtime_enabled: bool          # the user's on/off switch
    active: bool                   # capability AND runtime — effective state
    active_reason: str
    trading_mode: str
    is_paper: bool
    min_confidence: float
    monitor_interval_sec: int


class AutomationEventOut(BaseModel):
    id: int
    ts: datetime
    event_type: str
    actor: str
    intent_id: int | None
    order_id: int | None
    payload: dict


class RiskStateOut(BaseModel):
    trading_day: str
    start_equity: float
    current_equity: float
    day_pnl_pct: float
    daily_loss_limit_pct: float
    trading_halted: bool
    halted_reason: str | None
