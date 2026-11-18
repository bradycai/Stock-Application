"""Database models: watchlist, account snapshots, analysis runs, trade intents,
orders, append-only audit log, and daily risk state."""
import enum
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Float,
    UniqueConstraint,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy import false as sa_false
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class IntentAction(str, enum.Enum):
    buy = "buy"
    sell = "sell"
    watch = "watch"
    avoid = "avoid"


class IntentStatus(str, enum.Enum):
    recommended = "recommended"
    rejected_by_risk = "rejected_by_risk"
    pending_approval = "pending_approval"
    rejected_by_user = "rejected_by_user"
    approved = "approved"
    submitted = "submitted"
    filled = "filled"
    order_failed = "order_failed"
    canceled = "canceled"


class Watchlist(Base):
    __tablename__ = "watchlist"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ticker: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class AccountSnapshot(Base):
    __tablename__ = "account_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    equity: Mapped[float] = mapped_column(Numeric(18, 2), nullable=False)
    cash: Mapped[float] = mapped_column(Numeric(18, 2), nullable=False)
    buying_power: Mapped[float] = mapped_column(Numeric(18, 2), nullable=False)
    day_pnl: Mapped[float] = mapped_column(Numeric(18, 2), nullable=False)
    day_pnl_pct: Mapped[float] = mapped_column(Numeric(8, 4), nullable=False)
    open_positions: Mapped[int] = mapped_column(Integer, nullable=False)
    is_paper: Mapped[bool] = mapped_column(Boolean, nullable=False)
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False)


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("account_snapshots.id"))
    # The exact market data the model saw (replayable); null for runs before Phase 2.
    market_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("market_snapshots.id"))
    model: Mapped[str] = mapped_column(String, nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    input_context: Mapped[dict] = mapped_column(JSONB, nullable=False)
    raw_response: Mapped[dict] = mapped_column(JSONB, nullable=False)

    intents: Mapped[list["TradeIntent"]] = relationship(back_populates="run")


class TradeIntent(Base):
    __tablename__ = "trade_intents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("analysis_runs.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    ticker: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[IntentAction] = mapped_column(Enum(IntentAction, name="intent_action"), nullable=False)
    order_type: Mapped[str] = mapped_column(String, default="limit", nullable=False)
    limit_price: Mapped[float | None] = mapped_column(Numeric(18, 4))
    position_size_percent: Mapped[float | None] = mapped_column(Numeric(8, 4))
    stop_loss_percent: Mapped[float | None] = mapped_column(Numeric(8, 4))
    take_profit_percent: Mapped[float | None] = mapped_column(Numeric(8, 4))
    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4))
    reason: Mapped[str | None] = mapped_column(Text)
    risks: Mapped[str | None] = mapped_column(Text)

    status: Mapped[IntentStatus] = mapped_column(
        Enum(IntentStatus, name="intent_status"), default=IntentStatus.recommended, nullable=False
    )
    risk_passed: Mapped[bool | None] = mapped_column(Boolean)
    risk_violations: Mapped[dict | None] = mapped_column(JSONB)
    computed_qty: Mapped[int | None] = mapped_column(Integer)
    computed_notional: Mapped[float | None] = mapped_column(Numeric(18, 2))

    approved_by: Mapped[str | None] = mapped_column(String)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_live: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    auto_executed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Archiving hides an intent from the UI without a destructive delete (audit stays).
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    run: Mapped["AnalysisRun"] = relationship(back_populates="intents")
    orders: Mapped[list["Order"]] = relationship(back_populates="intent")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    intent_id: Mapped[int] = mapped_column(ForeignKey("trade_intents.id"), nullable=False)
    broker_order_id: Mapped[str | None] = mapped_column(String, unique=True)
    client_order_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    symbol: Mapped[str] = mapped_column(String, nullable=False)
    side: Mapped[str] = mapped_column(String, nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    order_type: Mapped[str] = mapped_column(String, nullable=False)
    limit_price: Mapped[float | None] = mapped_column(Numeric(18, 4))
    stop_loss_price: Mapped[float | None] = mapped_column(Numeric(18, 4))
    take_profit_price: Mapped[float | None] = mapped_column(Numeric(18, 4))

    status: Mapped[str] = mapped_column(String, nullable=False)
    filled_qty: Mapped[int] = mapped_column(Integer, default=0)
    filled_avg_price: Mapped[float | None] = mapped_column(Numeric(18, 4))
    is_live: Mapped[bool] = mapped_column(Boolean, nullable=False)
    raw: Mapped[dict | None] = mapped_column(JSONB)

    # Position-monitor tracking
    exit_reason: Mapped[str | None] = mapped_column(String)        # stop_loss|take_profit|daily_loss
    monitoring_status: Mapped[str | None] = mapped_column(String)  # active|exited
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    intent: Mapped["TradeIntent"] = relationship(back_populates="orders")


class AuditLog(Base):
    """Append-only. The app DB role should have no UPDATE/DELETE grant here."""
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    intent_id: Mapped[int | None] = mapped_column(ForeignKey("trade_intents.id"))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)


class PositionManagement(Base):
    """Per-position AI exit-management state: trailing stop, partials taken, evolving
    thesis/confidence. One open row per held symbol; closed when the position exits."""
    __tablename__ = "position_management"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False, index=True)
    is_open: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    entry_price: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    original_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    highest_price: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    initial_stop_price: Mapped[float | None] = mapped_column(Numeric(18, 4))
    trailing_stop_price: Mapped[float | None] = mapped_column(Numeric(18, 4))
    broker_stop_price: Mapped[float | None] = mapped_column(Numeric(18, 4))

    original_thesis: Mapped[str | None] = mapped_column(Text)
    current_thesis: Mapped[str | None] = mapped_column(Text)
    current_confidence: Mapped[float | None] = mapped_column(Numeric(5, 4))
    exit_condition: Mapped[str | None] = mapped_column(Text)
    last_decision: Mapped[str | None] = mapped_column(String)
    last_analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Partial-profit levels already realized, e.g. [5, 10]
    partial_levels_taken: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    intent_id: Mapped[int | None] = mapped_column(ForeignKey("trade_intents.id"))


class AutomationState(Base):
    """Singleton (id=1) runtime automation state, set via the API.

    `mode` is the user's choice: manual (default — nothing is automated), observe
    (automation evaluates and logs what it WOULD do, but places no orders), or auto. An
    automated order needs BOTH this row (mode == "auto", kill switch off) AND the
    environment (AUTO_TRADING_ENABLED, plus the live confirmation when live). The kill
    switch persists until released and also blocks new buys from manual approval.
    """
    __tablename__ = "automation_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1, autoincrement=False)
    mode: Mapped[str] = mapped_column(String, default="manual", server_default="manual", nullable=False)
    kill_switch: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_false(),
                                              nullable=False)
    kill_switch_reason: Mapped[str | None] = mapped_column(String)
    kill_switch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    kill_switch_by: Mapped[str | None] = mapped_column(String)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_by: Mapped[str | None] = mapped_column(String)
    # Automation-event feed is hidden (not deleted) before this timestamp in the UI.
    events_cleared_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RiskState(Base):
    __tablename__ = "risk_state"

    trading_day: Mapped[date] = mapped_column(Date, primary_key=True)
    start_equity: Mapped[float] = mapped_column(Numeric(18, 2), nullable=False)
    trading_halted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    halted_reason: Mapped[str | None] = mapped_column(String)
    halted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LLMCall(Base):
    """One row per Claude request: the token and cost ledger (append-only).

    `run_id` correlates every call made by one analysis run or monitor cycle. Cost is an
    estimate from list prices (see app.services.llm.PRICES), not an invoice.
    """
    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    run_id: Mapped[str | None] = mapped_column(String, index=True)
    role: Mapped[str] = mapped_column(String, nullable=False)
    prompt_name: Mapped[str] = mapped_column(String, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String, nullable=False)
    requested_model: Mapped[str] = mapped_column(String, nullable=False)
    served_model: Mapped[str | None] = mapped_column(String)
    effort: Mapped[str | None] = mapped_column(String)
    # ok | refusal | incomplete | invalid_output | api_error
    status: Mapped[str] = mapped_column(String, nullable=False)
    stop_reason: Mapped[str | None] = mapped_column(String)
    refusal_category: Mapped[str | None] = mapped_column(String)
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6))
    elapsed_ms: Mapped[int | None] = mapped_column(Integer)
    request_id: Mapped[str | None] = mapped_column(String)
    error: Mapped[str | None] = mapped_column(Text)



# --------------------------------------------------------------------------- #
# Phase 2: market data and research history                                   #
# --------------------------------------------------------------------------- #
class MarketSnapshot(Base):
    """The exact, timestamped market data one analysis/monitor run used. `data` is the full
    context sent to the model (portfolio, per-symbol quotes/indicators/news, market
    context), so a run can be replayed later; `quality` holds the per-symbol stale /
    incomplete / missing-field flags computed before any model saw the data."""
    __tablename__ = "market_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    run_id: Mapped[str | None] = mapped_column(String, index=True)
    purpose: Mapped[str] = mapped_column(String, nullable=False)        # analysis | monitor | watcher
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # collection start
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    feed: Mapped[str] = mapped_column(String, nullable=False)
    market_open: Mapped[bool | None] = mapped_column(Boolean)
    symbols: Mapped[list] = mapped_column(JSONB, nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    quality: Mapped[dict] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)


class MarketBar(Base):
    """Price bars as received (one row per symbol/timeframe/bar time/feed). Append-only
    history for replay and backtesting; refetched bars update in place."""
    __tablename__ = "market_bars"
    __table_args__ = (UniqueConstraint("symbol", "timeframe", "ts", "feed", name="uq_market_bars_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False, index=True)
    timeframe: Mapped[str] = mapped_column(String, nullable=False)       # 1Day | 1Min
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False)
    vwap: Mapped[float | None] = mapped_column(Float)
    trade_count: Mapped[float | None] = mapped_column(Float)
    feed: Mapped[str] = mapped_column(String, nullable=False)
    adjustment: Mapped[str] = mapped_column(String, nullable=False, default="raw")
    source: Mapped[str] = mapped_column(String, nullable=False, default="alpaca")
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LatestQuote(Base):
    """Most recent observed quote/trade per symbol (from snapshots or the stream)."""
    __tablename__ = "latest_quotes"

    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    bid: Mapped[float | None] = mapped_column(Float)
    ask: Mapped[float | None] = mapped_column(Float)
    bid_size: Mapped[float | None] = mapped_column(Float)
    ask_size: Mapped[float | None] = mapped_column(Float)
    quote_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_trade_price: Mapped[float | None] = mapped_column(Float)
    last_trade_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    feed: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)          # snapshot | stream
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())


class NewsEvent(Base):
    """A news article, stored once. `dedup_hash` collapses the same story arriving from
    different sources or for different symbols; `event_type` is a deterministic keyword
    classification (versioned) — model-based extraction fills `extraction` later."""
    __tablename__ = "news_events"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_news_events_source_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str] = mapped_column(String, nullable=False)           # alpaca | newsapi
    external_id: Mapped[str] = mapped_column(String, nullable=False)
    publisher: Mapped[str | None] = mapped_column(String)                 # e.g. benzinga
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)                     # full text when available
    url: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String)
    symbols: Mapped[list] = mapped_column(JSONB, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    first_seen_run_id: Mapped[str | None] = mapped_column(String)
    dedup_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    classifier_version: Mapped[str] = mapped_column(String, nullable=False)
    extraction: Mapped[dict | None] = mapped_column(JSONB)
