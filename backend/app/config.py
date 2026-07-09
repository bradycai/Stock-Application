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
    claude_model: str = "claude-opus-4-8"

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
