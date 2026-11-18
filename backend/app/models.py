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
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
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

    # Provenance (issue #4): which strategy/prompt proposed it, on what evidence and data.
    strategy_id: Mapped[str | None] = mapped_column(String)
    strategy_version: Mapped[str | None] = mapped_column(String)
    evidence: Mapped[list | None] = mapped_column(JSONB)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    market_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("market_snapshots.id"))
    prompt_version: Mapped[str | None] = mapped_column(String)

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


# --------------------------------------------------------------------------- #
# Trade journal (issue #4)                                                    #
# --------------------------------------------------------------------------- #
class SignalEvent(Base):
    """A deterministic signal observed by the watcher; `event_key` dedupes repeats."""
    __tablename__ = "signal_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    event_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    run_id: Mapped[str | None] = mapped_column(String, index=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)


class OrderFill(Base):
    """Append-only history of an order's broker state; a row is added only when the
    (status, filled_qty, filled_avg_price) state changes, so repeated syncs don't duplicate."""
    __tablename__ = "order_fills"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False, index=True)
    broker_order_id: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    requested_qty: Mapped[int | None] = mapped_column(Integer)
    filled_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    filled_avg_price: Mapped[float | None] = mapped_column(Numeric(18, 4))
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict | None] = mapped_column(JSONB)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TradeOutcome(Base):
    """Realized result of one executed trade (entry fill to exit fill), vs SPY."""
    __tablename__ = "trade_outcomes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    intent_id: Mapped[int] = mapped_column(ForeignKey("trade_intents.id"), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False, index=True)
    entry_order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), unique=True)
    exit_order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    entry_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    entry_price: Mapped[float | None] = mapped_column(Numeric(18, 4))
    exit_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_price: Mapped[float | None] = mapped_column(Numeric(18, 4))
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    realized_pnl: Mapped[float | None] = mapped_column(Numeric(18, 2))
    realized_pnl_pct: Mapped[float | None] = mapped_column(Numeric(10, 4))
    holding_days: Mapped[float | None] = mapped_column(Float)
    exit_reason: Mapped[str | None] = mapped_column(String)
    benchmark_symbol: Mapped[str] = mapped_column(String, default="SPY", nullable=False)
    benchmark_return_pct: Mapped[float | None] = mapped_column(Numeric(10, 4))
    status: Mapped[str] = mapped_column(String, nullable=False)          # open | closed
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CandidateOutcome(Base):
    """Hypothetical forward return of an intent (any status, incl. rejected/watch)."""
    __tablename__ = "candidate_outcomes"
    __table_args__ = (
        UniqueConstraint("intent_id", "horizon_days", "rule_version", name="uq_candidate_outcomes_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    intent_id: Mapped[int] = mapped_column(ForeignKey("trade_intents.id"), nullable=False, index=True)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)
    rule_version: Mapped[str] = mapped_column(String, nullable=False)
    start_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    start_price: Mapped[float] = mapped_column(Float, nullable=False)
    end_price: Mapped[float] = mapped_column(Float, nullable=False)
    return_pct: Mapped[float] = mapped_column(Float, nullable=False)
    benchmark_return_pct: Mapped[float | None] = mapped_column(Float)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StrategyMetric(Base):
    """Aggregated performance per strategy/version/window (computed in a later PR)."""
    __tablename__ = "strategy_metrics"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    strategy_version: Mapped[str | None] = mapped_column(String)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metrics: Mapped[dict] = mapped_column(JSONB, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReviewReport(Base):
    """A periodic performance/decision review (generated in a later PR)."""
    __tablename__ = "review_reports"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str] = mapped_column(String, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    llm_run_id: Mapped[str | None] = mapped_column(String)


# Research data (issue #6)
class Company(Base):
    """Reference data per symbol (SEC EDGAR): CIK, SIC and a derived sector."""
    __tablename__ = "companies"

    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    cik: Mapped[str | None] = mapped_column(String)
    name: Mapped[str | None] = mapped_column(String)
    sic: Mapped[str | None] = mapped_column(String)
    sic_description: Mapped[str | None] = mapped_column(String)
    sector: Mapped[str | None] = mapped_column(String)
    industry: Mapped[str | None] = mapped_column(String)
    exchange: Mapped[str | None] = mapped_column(String)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())


class Fundamental(Base):
    """One XBRL fact. `filed_at` is the release date: use it (not period_end) for point-in-time."""
    __tablename__ = "fundamentals"
    __table_args__ = (UniqueConstraint("symbol", "metric", "period_end", "form", name="uq_fundamentals_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False, index=True)
    cik: Mapped[str | None] = mapped_column(String)
    metric: Mapped[str] = mapped_column(String, nullable=False)  # revenue | net_income | eps_diluted | ...
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    fiscal_period: Mapped[str | None] = mapped_column(String)    # FY | Q1 | Q2 | Q3
    form: Mapped[str] = mapped_column(String, nullable=False)    # 10-K | 10-Q
    value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str | None] = mapped_column(String)
    filed_at: Mapped[date | None] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String, nullable=False, default="sec_edgar")
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Filing(Base):
    __tablename__ = "filings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False, index=True)
    cik: Mapped[str | None] = mapped_column(String)
    accession: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    form: Mapped[str] = mapped_column(String, nullable=False)
    filed_at: Mapped[date | None] = mapped_column(Date)
    period_of_report: Mapped[date | None] = mapped_column(Date)
    url: Mapped[str | None] = mapped_column(Text)


class MacroObservation(Base):
    """A FRED observation. Vintages (realtime_start) are kept for point-in-time backtests."""
    __tablename__ = "macro_observations"
    __table_args__ = (UniqueConstraint("series_id", "observation_date", "realtime_start",
                                       name="uq_macro_obs_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    series_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    observation_date: Mapped[date] = mapped_column(Date, nullable=False)
    value: Mapped[float | None] = mapped_column(Float)
    realtime_start: Mapped[date] = mapped_column(Date, nullable=False)
    realtime_end: Mapped[date | None] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String, nullable=False, default="fred")
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# Market watcher (issue #12)
class WatcherDecision(Base):
    """One validated Haiku watcher assessment of a changed symbol."""
    __tablename__ = "watcher_decisions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 index=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False, index=True)
    action: Mapped[str] = mapped_column(String, nullable=False)  # IGNORE|WATCH|INVESTIGATE|...
    event_type: Mapped[str | None] = mapped_column(String)
    urgency: Mapped[str] = mapped_column(String, nullable=False)  # low | medium | high
    evidence_ids: Mapped[list] = mapped_column(JSONB, nullable=False)
    explanation: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    prompt_version: Mapped[str | None] = mapped_column(String)
    model: Mapped[str | None] = mapped_column(String)
    run_id: Mapped[str | None] = mapped_column(String, index=True)
    signal_event_ids: Mapped[list] = mapped_column(JSONB, nullable=False)
    downgraded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class ResearchJob(Base):
    """Queue of follow-up work created by the watcher (consumers arrive in a later PR)."""
    __tablename__ = "research_jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)    # INVESTIGATE | REVIEW_POSITION
    symbol: Mapped[str] = mapped_column(String, nullable=False, index=True)
    signal_event_ids: Mapped[list] = mapped_column(JSONB, nullable=False)
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("watcher_decisions.id"))
    status: Mapped[str] = mapped_column(String, nullable=False, default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    leased_by: Mapped[str | None] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_beat: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False)


class WatcherSymbolState(Base):
    """Per-symbol previous observation the watcher compares against."""
    __tablename__ = "watcher_symbol_state"

    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    last_news_id: Mapped[int | None] = mapped_column(BigInteger)
    last_price: Mapped[float | None] = mapped_column(Float)
    last_price_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_bar_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    regime: Mapped[str | None] = mapped_column(String)
    position_fingerprint: Mapped[str | None] = mapped_column(String)
    last_assessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_action: Mapped[str | None] = mapped_column(String)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())


# --------------------------------------------------------------------------- #
# Research pipeline (Phase 5 contract — app/services/research/contracts.py)   #
# --------------------------------------------------------------------------- #
class ResearchBundle(Base):
    """One frozen, timestamped evidence bundle for one research job. Analysts and the
    strategist only ever see this bundle, so every report and proposal is replayable."""
    __tablename__ = "research_bundles"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    job_id: Mapped[int | None] = mapped_column(ForeignKey("research_jobs.id"), index=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)              # INVESTIGATE | REVIEW_POSITION
    status: Mapped[str] = mapped_column(String, nullable=False, default="collecting")
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    holding: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    market_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("market_snapshots.id"))
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)          # {id: EvidenceItem dict}
    context: Mapped[dict] = mapped_column(JSONB, nullable=False)           # portfolio exposure, etc.
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    leased_by: Mapped[str | None] = mapped_column(String)
    error: Mapped[str | None] = mapped_column(Text)


class AnalystReportRow(Base):
    __tablename__ = "analyst_reports"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    bundle_id: Mapped[int] = mapped_column(ForeignKey("research_bundles.id"), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    analyst: Mapped[str] = mapped_column(String, nullable=False)
    skill_version: Mapped[str | None] = mapped_column(String)
    model: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False)            # ok | failed | rejected
    report: Mapped[dict | None] = mapped_column(JSONB)                     # AnalystReport dict
    error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("bundle_id", "analyst", name="uq_analyst_reports_bundle_analyst"),)


class StrategyProposal(Base):
    """Every strategist proposal — converted, rejected, abstained or expired alike."""
    __tablename__ = "strategy_proposals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    bundle_id: Mapped[int] = mapped_column(ForeignKey("research_bundles.id"), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False, index=True)
    action: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)            # see PROPOSAL_STATUSES
    strategy_id: Mapped[str] = mapped_column(String, nullable=False, default="opus-strategist")
    strategy_version: Mapped[str | None] = mapped_column(String)          # prompt/skill version
    model: Mapped[str | None] = mapped_column(String)
    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4))
    quote_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    proposal: Mapped[dict] = mapped_column(JSONB, nullable=False)          # StrategistProposal dict
    validation: Mapped[dict] = mapped_column(JSONB, nullable=False)        # violations / notes
    intent_id: Mapped[int | None] = mapped_column(ForeignKey("trade_intents.id"))


# --- ops / system alerts (PR 10) ---
class SystemAlert(Base):
    """An operational alert raised by the health watcher. At most one open or acked row per
    (kind, key): repeats bump `count`/`last_seen`; it resolves when the condition clears."""
    __tablename__ = "system_alerts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    key: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False)          # info | warning | critical
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String, nullable=False, default="open")  # open | acked | resolved
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        Index("ix_system_alerts_status_last_seen", "status", "last_seen"),
        # dedup while active: one non-resolved row per (kind, key)
        Index("uq_system_alerts_active", "kind", "key", unique=True,
              postgresql_where=text("status <> 'resolved'")),
    )


# --- scheduled jobs (recurring jobs) ---
class ScheduledJobRun(Base):
    """One run of a recurring job for one period. Unique (job_name, period_key): the insert-or-skip
    claim makes runs idempotent across workers and restarts."""
    __tablename__ = "scheduled_job_runs"
    __table_args__ = (
        UniqueConstraint("job_name", "period_key", name="uq_scheduled_job_runs_period"),
        Index("ix_scheduled_job_runs_job_started", "job_name", "started_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_name: Mapped[str] = mapped_column(String, nullable=False)
    period_key: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)   # running|succeeded|failed|skipped
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
