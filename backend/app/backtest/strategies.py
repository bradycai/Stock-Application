"""Strategy interface and baseline strategies.

A strategy sees only a `PointInTimeView` and a read-only `StrategyContext` at each close and
returns entry `Signal`s. It never sizes orders: the engine sizes every entry through
`risk.evaluate`. `ReplayStrategy` replays pre-recorded (e.g. cached model) signals by date so
AI strategies can be backtested later without calling a model.
"""
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from app.backtest.data import PointInTimeView
from app.services.indicators import technical_indicators
from app.services.prescreener import rank_candidates


@dataclass(frozen=True)
class Signal:
    symbol: str
    stop_loss_pct: float
    take_profit_pct: float | None = None
    max_hold_days: int | None = None
    position_size_pct: float | None = None   # None -> BacktestConfig default
    limit_offset_pct: float | None = None    # buy limit = close * (1 + offset/100)
    reason: str = ""


@dataclass(frozen=True)
class StrategyContext:
    date: date
    equity: float
    cash: float
    held: dict[str, int] = field(default_factory=dict)       # symbol -> trading days held
    pending: tuple[str, ...] = ()                            # symbols with an unfilled entry order
    day_index: int = 0                                       # trading days since window start


class Strategy(Protocol):
    name: str

    def on_close(self, view: PointInTimeView, ctx: StrategyContext) -> list[Signal]: ...


@dataclass
class MomentumStrategy:
    """Every `rebalance_days` trading days, rank the universe with the existing prescreener on
    point-in-time indicators and propose the top `top_n` not already held."""
    universe: list[str]
    top_n: int = 3
    rebalance_days: int = 5
    stop_loss_pct: float = 5.0
    take_profit_pct: float = 10.0
    max_hold_days: int = 20
    min_score: float = 0.0
    min_history: int = 30
    lookback: int = 300
    name: str = "momentum"

    def on_close(self, view: PointInTimeView, ctx: StrategyContext) -> list[Signal]:
        if ctx.day_index % max(1, self.rebalance_days) != 0:
            return []
        market = {}
        for sym in self.universe:
            bars = view.bars(sym, lookback=self.lookback)
            if len(bars) < self.min_history:
                continue
            last = bars[-1]
            market[sym] = {
                "indicators": technical_indicators(bars),
                "intraday_change_pct": (last.close / last.open - 1) * 100 if last.open else 0.0,
                "news": [],
            }
        out = []
        for r in rank_candidates(market, len(market)):
            if len(out) >= self.top_n:
                break
            if r.score < self.min_score or r.symbol in ctx.held or r.symbol in ctx.pending:
                continue
            out.append(Signal(r.symbol, self.stop_loss_pct, self.take_profit_pct, self.max_hold_days,
                              reason=f"momentum score {r.score}"))
        return out


@dataclass
class ReplayStrategy:
    """Replays recorded signals keyed by decision date (e.g. cached model outputs)."""
    signals_by_date: dict[date, list[Signal]]
    name: str = "replay"

    def on_close(self, view: PointInTimeView, ctx: StrategyContext) -> list[Signal]:
        return list(self.signals_by_date.get(ctx.date, []))
