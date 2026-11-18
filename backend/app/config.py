"""Environment-driven settings. Secrets live here and never leave the server."""
from decimal import Decimal
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/trading_assistant"

    # Claude
    anthropic_api_key: str = ""

    # Model routing (one model + effort per layer). Every Claude request goes through
    # app.services.llm, which applies these.
    #   strategist — trade analysis (proposes buy/sell/watch/avoid intents)
    #   position   — continuous-monitor and open-position decisions (empty = strategist model)
    #   research   — specialist analysts (later phases)
    #   watcher    — high-frequency event watcher (later phases)
    strategist_model: str = "claude-opus-5-5"
    strategist_effort: str = "high"
    strategist_max_tokens: int = 16000
    position_model: str = ""
    position_effort: str = "medium"
    position_max_tokens: int = 8000
    research_model: str = "claude-sonnet-5-5"
    research_effort: str = "medium"
    research_max_tokens: int = 8000
    watcher_model: str = "claude-haiku-5-5"
    watcher_effort: str = "low"
    watcher_max_tokens: int = 4000

    # Claude request controls
    llm_timeout_sec: float = 300.0       # per attempt
    llm_max_retries: int = 2             # SDK retries on 408/409/429/5xx and network errors
    llm_refusal_fallbacks: bool = True   # server-side fallback on safety refusals, where supported

    # Fail-closed freshness and sanity limits for model output
    max_data_age_sec: int = 600          # market snapshot older than this -> no actionable intents
    intent_max_age_min: int = 60         # pending intents older than this can't be approved
    max_limit_deviation_pct: Decimal = Decimal("10")  # limit price vs. last price at analysis time

    # Alpaca
    alpaca_key: str = ""
    alpaca_secret: str = ""
    trading_mode: str = "paper"  # "paper" | "live"

    # Auth
    api_token: str = "change-me"

    # Risk parameters
    daily_loss_limit_pct: Decimal = Decimal("10")
    max_position_pct: Decimal = Decimal("10")
    max_open_positions: int = 3
    penny_stock_price: Decimal = Decimal("5.00")
    max_intraday_gain_pct: Decimal = Decimal("50")

    # Prescreen + batching pipeline (large-watchlist support)
    max_watchlist_size: int = 500       # hard cap on tickers fetched/screened per run
    prescreen_top_n: int = 30           # how many top-ranked candidates go to Claude
    claude_batch_size: int = 10         # tickers per Claude call
    max_final_recommendations: int = 30 # cap on merged recommendations returned

    # Optional data sources
    news_api_key: str = ""

    # Research data (SEC EDGAR fundamentals, FRED macro). Both are refreshed only by the CLI
    # (python -m app.services.research_data.refresh), never inside a web request.
    sec_user_agent: str = ""            # SEC needs a descriptive UA with contact; "" = EDGAR off
    fred_api_key: str = ""              # "" = FRED disabled
    sec_max_requests_per_sec: float = 5.0   # client-side limit (SEC allows 10; we stay at <= 5)
    research_http_timeout_sec: float = 20.0

    # Market data layer (batched Alpaca requests + snapshot store)
    alpaca_data_feed: str = ""          # "" = the account's default feed (IEX on free plans); "iex" | "sip"
    data_chunk_size: int = 100          # symbols per batched Alpaca request
    data_max_concurrency: int = 4       # parallel batched requests (bounded to respect rate limits)
    bars_cache_ttl_sec: int = 900       # in-process cache for daily bars
    max_quote_age_sec: int = 120        # during market hours, an older quote is flagged stale
    news_lookback_hours: int = 48
    news_per_symbol: int = 5
    # ETFs used for the market-context block (index trend, sector breadth, volatility proxy).
    market_context_symbols: str = "SPY,QQQ,IWM,DIA,XLK,XLF,XLE,XLV,XLY,XLP,XLI,XLB,XLU,XLRE,XLC"

    # Streaming worker (python -m app.workers.market_stream)
    stream_flush_sec: float = 2.0            # latest-quote / bar / news buffers flush this often
    stream_refill_sec: float = 300.0         # REST refill of 1Min bars missed while disconnected
    stream_refill_max_minutes: int = 60      # never look back further than this on a refill
    stream_symbol_refresh_sec: float = 180.0 # re-read watchlist + open positions this often
    stream_heartbeat_sec: float = 60.0       # structured heartbeat log interval

    # Autonomous trading (all default OFF — manual approval is unchanged when off)
    auto_trading_enabled: bool = False
    # Required, in addition to TRADING_MODE=live and auto_trading_enabled, before any
    # autonomous LIVE order is permitted. Must equal exactly "I_UNDERSTAND_THE_RISK".
    auto_live_trading_confirmation: str = ""
    # Minimum Claude confidence (in percent, 0-100) for an auto-buy/sell.
    auto_min_confidence: Decimal = Decimal("75")
    # Position-monitor poll interval (seconds). Clamped to [60, 300] = 1-5 minutes.
    auto_monitor_interval_sec: int = 60

    # Continuous AI market monitor (rule-based triggers -> Claude). OFF by default.
    continuous_monitor_enabled: bool = False
    monitor_interval_sec: int = 60          # loop cadence (clamped to [30, 300])
    auto_sell_confidence: Decimal = Decimal("65")  # min confidence (%) to auto-sell/trim
    auto_buy_confidence: Decimal = Decimal("50")   # min confidence (%) to auto-buy/add
    monitor_max_symbols: int = 40           # cap monitored tickers per cycle; 0 = no cap (all)

    # AI-managed exits (trailing stop + partial profit-taking + thesis re-evaluation)
    ai_exit_confidence: Decimal = Decimal("65")   # min Claude confidence (%) to AI-sell
    trailing_stop_percent: Decimal = Decimal("6")  # fallback trailing distance when no ATR
    partial_profit_levels: str = "5,10,15"         # gain % thresholds for partial sells
    partial_profit_amounts: str = "20,20,20"       # % of original qty to sell at each level
    confidence_recheck_interval: int = 300         # force a Claude re-review every N seconds
    max_hold_days: int = 20                        # flag/review positions held longer than this

    @property
    def partial_levels(self) -> list[float]:
        return [float(x) for x in self.partial_profit_levels.split(",") if x.strip()]

    @property
    def partial_amounts(self) -> list[float]:
        return [float(x) for x in self.partial_profit_amounts.split(",") if x.strip()]

    # CORS
    frontend_origin: str = "http://localhost:5173"

    # Logging: INFO-level structured logs. LOG_FORMAT=json (default) or text.
    log_level: str = "INFO"
    log_format: str = "json"

    @property
    def is_live(self) -> bool:
        return self.trading_mode.lower() == "live"

    @property
    def live_auto_confirmed(self) -> bool:
        return self.auto_live_trading_confirmation == "I_UNDERSTAND_THE_RISK"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
