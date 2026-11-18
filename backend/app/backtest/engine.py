"""Portfolio simulator.

Daily loop for each simulated date d:
  1. scheduled time exits (max-hold) sell at d's open
  2. entry orders placed at the previous close try to fill on d (day limit orders)
  3. stops / targets are evaluated intrabar (stop wins ties; no target on the entry bar)
  4. mark to market at d's close, equity point recorded
  5. the strategy sees a point-in-time view through d and proposes entries; every entry is
     sized by `risk.evaluate` against the simulated account (max positions, max position %,
     cash only, daily-loss halt) and becomes a pending order for d+1.
Open positions are liquidated at the last close so every trade appears in the log.
"""
import math
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from decimal import Decimal

from app.backtest.data import BarStore
from app.backtest.fills import (
    CostModel,
    Fill,
    evaluate_exit,
    fill_buy_limit,
    fill_market_at_close,
    fill_market_at_open,
)
from app.backtest.strategies import Signal, Strategy, StrategyContext
from app.config import settings
from app.services import market_context
from app.services.risk import RiskInput, evaluate

SECTOR_ETFS = ("XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB", "XLRE", "XLC")
CONTEXT_SYMBOLS = ("SPY", "QQQ", "IWM", "DIA") + SECTOR_ETFS


@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: float = 100_000.0
    benchmark: str = "SPY"
    top_n: int = 3
    rebalance_days: int = 5
    stop_loss_pct: float = 5.0
    take_profit_pct: float = 10.0
    max_hold_days: int = 20
    position_size_pct: float = float(settings.max_position_pct)
    entry_limit_offset_pct: float = 0.5
    daily_loss_limit_pct: float = float(settings.daily_loss_limit_pct)
    slippage_bps: float = 5.0
    fee_bps: float = 0.0
    fee_per_share: float = 0.0
    max_volume_pct: float = 10.0
    seed: int = 0
    bootstrap_samples: int = 1000

    def cost_model(self) -> CostModel:
        return CostModel(self.slippage_bps, self.fee_bps, self.fee_per_share, self.max_volume_pct)

    def with_params(self, **params) -> "BacktestConfig":
        return replace(self, **params)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Trade:
    symbol: str
    entry_date: date
    exit_date: date
    qty: int
    entry_price: float
    exit_price: float
    reason: str
    pnl: float
    return_pct: float
    fees: float
    days_held: int
    regime: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["entry_date"], d["exit_date"] = self.entry_date.isoformat(), self.exit_date.isoformat()
        return d


@dataclass
class _Order:
    symbol: str
    limit: float
    qty: int
    signal: Signal
    regime: str


@dataclass
class _Position:
    symbol: str
    entry_date: date
    qty: int
    entry_qty: int
    entry_price: float
    stop: float
    target: float | None
    max_hold_days: int | None
    regime: str
    cost_basis: float          # entry notional + entry fee
    proceeds: float = 0.0      # net of exit fees
    fees: float = 0.0
    exit_qty: int = 0
    exit_notional: float = 0.0
    days_held: int = 0
    time_exit: bool = False
    last_reason: str = ""


@dataclass
class RunResult:
    equity_curve: list[tuple[date, float]]
    trades: list[Trade]
    exposure: list[float]
    traded_notional: float
    regime_by_date: dict[date, str]
    rejections: dict[str, int] = field(default_factory=dict)
    halted_days: list[date] = field(default_factory=list)
    signals: int = 0


def regime_at(view, symbols: list[str]) -> str:
    """Point-in-time regime_hint from market_context over the context ETFs available in `view`."""
    avail = [s for s in symbols if view.last_bar(s) is not None]
    if "SPY" not in avail:
        return "unknown"
    bars = {s: view.bars(s, lookback=260) for s in avail}
    return market_context.compute(bars, avail)["regime_hint"]


def _dec(x: float) -> Decimal:
    return Decimal(str(round(x, 6)))


def run_backtest(store: BarStore, strategy: Strategy, universe: list[str], dates: list[date],
                 config: BacktestConfig) -> RunResult:
    cost = config.cost_model()
    cash = config.initial_cash
    positions: dict[str, _Position] = {}
    pending: list[_Order] = []
    trades: list[Trade] = []
    curve: list[tuple[date, float]] = []
    exposure: list[float] = []
    traded = 0.0
    regimes: dict[date, str] = {}
    rejections: dict[str, int] = {}
    halted_days: list[date] = []
    n_signals = 0
    last_close: dict[str, float] = {}
    prev_equity = config.initial_cash
    ctx_syms = [s for s in CONTEXT_SYMBOLS if store.has(s)]

    def close_out(pos: _Position, d: date) -> None:
        pnl = pos.proceeds - pos.cost_basis
        trades.append(Trade(
            pos.symbol, pos.entry_date, d, pos.entry_qty, round(pos.entry_price, 4),
            round(pos.exit_notional / pos.exit_qty, 4), pos.last_reason, round(pnl, 2),
            round(pnl / pos.cost_basis * 100, 4), round(pos.fees, 4), pos.days_held, pos.regime))
        del positions[pos.symbol]

    def apply_sell(pos: _Position, fill: Fill, d: date) -> None:
        nonlocal cash, traded
        net = fill.notional - fill.fee
        cash += net
        traded += fill.notional
        pos.qty -= fill.qty
        pos.proceeds += net
        pos.fees += fill.fee
        pos.exit_qty += fill.qty
        pos.exit_notional += fill.notional
        pos.last_reason = fill.reason
        if pos.qty == 0:
            close_out(pos, d)

    for idx, d in enumerate(dates):
        bars = {s: b for s in store.symbols() if (b := store.bar_on(s, d)) is not None}

        # 1. scheduled time exits at the open
        for pos in list(positions.values()):
            if pos.time_exit and pos.symbol in bars:
                fill = fill_market_at_open("sell", pos.qty, bars[pos.symbol], cost, "max_hold")
                if fill:
                    apply_sell(pos, fill, d)

        # 2. entry orders from the previous close
        for order in pending:
            bar = bars.get(order.symbol)
            fill = fill_buy_limit(order.limit, order.qty, bar, cost) if bar else None
            if fill is None:
                continue
            qty = fill.qty
            while qty > 0 and qty * fill.price + cost.fee(qty, fill.price) > cash:
                qty -= 1
            if qty < 1:
                continue
            fee = cost.fee(qty, fill.price)
            cash -= qty * fill.price + fee
            traded += qty * fill.price
            sig = order.signal
            positions[order.symbol] = _Position(
                order.symbol, d, qty, qty, fill.price,
                stop=fill.price * (1 - sig.stop_loss_pct / 100),
                target=fill.price * (1 + sig.take_profit_pct / 100) if sig.take_profit_pct else None,
                max_hold_days=sig.max_hold_days, regime=order.regime,
                cost_basis=qty * fill.price + fee, fees=fee)
        pending = []

        # 3. intrabar stops / targets
        for pos in list(positions.values()):
            bar = bars.get(pos.symbol)
            if bar is None:
                continue
            fill = evaluate_exit(bar, pos.stop, pos.target, pos.qty, cost,
                                 allow_target=pos.entry_date != d)
            if fill:
                apply_sell(pos, fill, d)

        # 4. mark to market
        for s, b in bars.items():
            last_close[s] = b.close
        position_value = sum(p.qty * last_close.get(p.symbol, p.entry_price) for p in positions.values())
        equity = cash + position_value
        for pos in positions.values():
            pos.days_held += 1
            if pos.max_hold_days is not None and pos.days_held >= pos.max_hold_days:
                pos.time_exit = True
        curve.append((d, equity))
        exposure.append(position_value / equity if equity > 0 else 0.0)

        if idx == len(dates) - 1:
            break  # no new decisions after the final bar

        # 5. decisions at the close, through the point-in-time view only
        view = store.view(d)
        regimes[d] = regime_at(view, ctx_syms) if ctx_syms else "unknown"
        day_pnl_pct = (equity / prev_equity - 1) * 100 if prev_equity else 0.0
        prev_equity = equity
        halted = day_pnl_pct <= -config.daily_loss_limit_pct
        if halted:
            halted_days.append(d)
        ctx = StrategyContext(d, equity, cash, {s: p.days_held for s, p in positions.items()},
                              (), idx)
        reserved = 0.0
        for sig in strategy.on_close(view, ctx):
            n_signals += 1
            bar = bars.get(sig.symbol)
            if bar is None:
                rejections["NO_BAR_TODAY"] = rejections.get("NO_BAR_TODAY", 0) + 1
                continue
            offset = config.entry_limit_offset_pct if sig.limit_offset_pct is None else sig.limit_offset_pct
            limit = round(bar.close * (1 + offset / 100), 2)
            intent = {
                "action": "buy", "ticker": sig.symbol, "order_type": "limit", "limit_price": str(limit),
                "stop_loss_percent": str(sig.stop_loss_pct),
                "position_size_percent": str(config.position_size_pct if sig.position_size_pct is None
                                             else sig.position_size_pct),
            }
            res = evaluate(RiskInput(
                intent=intent, equity=_dec(equity), cash=_dec(cash - reserved),
                open_positions=len(positions) + len(pending),
                held_symbols=set(positions) | {o.symbol for o in pending},
                day_pnl_pct=_dec(day_pnl_pct), last_price=_dec(bar.close),
                intraday_change_pct=_dec((bar.close / bar.open - 1) * 100 if bar.open else 0.0),
                is_margin_account=False, is_option=False, trading_halted=halted))
            if not res.passed or not res.computed_qty:
                for v in res.violations:
                    rejections[v.code] = rejections.get(v.code, 0) + 1
                continue
            reserved += res.computed_qty * limit
            pending.append(_Order(sig.symbol, limit, res.computed_qty, sig, regimes[d]))

    # liquidate leftovers at the last close
    if dates:
        last_d = dates[-1]
        for pos in list(positions.values()):
            bar = store.bar_on(pos.symbol, last_d)
            if bar is not None:
                fill = fill_market_at_close("sell", pos.qty, bar, cost, "end_of_data")
            else:
                px = last_close.get(pos.symbol, pos.entry_price)
                fill = Fill(pos.qty, px, cost.fee(pos.qty, px), "end_of_data")
            if fill is None:  # volume cap: force the exit at the last close
                px = last_close.get(pos.symbol, pos.entry_price)
                fill = Fill(pos.qty, px, cost.fee(pos.qty, px), "end_of_data")
            apply_sell(pos, fill, last_d)
        curve[-1] = (last_d, cash)
    return RunResult(curve, trades, exposure, traded, regimes, rejections, halted_days, n_signals)


def buy_and_hold(store: BarStore, symbol: str, dates: list[date], config: BacktestConfig
                 ) -> list[tuple[date, float]]:
    """Benchmark: buy at the first window open, hold, liquidate at the last close, same costs."""
    cost = config.cost_model()
    cash = config.initial_cash
    qty = 0
    curve: list[tuple[date, float]] = []
    last = None
    bought = False
    for d in dates:
        bar = store.bar_on(symbol, d)
        if bar is not None:
            last = bar.close
            if not bought:
                bought = True
                px = bar.open * (1 + cost.slippage_bps / 10_000)
                qty = math.floor(cash / (px * (1 + config.fee_bps / 10_000) + config.fee_per_share))
                cash -= qty * px + cost.fee(qty, px)
        curve.append((d, cash + qty * (last if last is not None else 0.0)))
    if curve and qty:
        px = last * (1 - cost.slippage_bps / 10_000)
        curve[-1] = (curve[-1][0], cash + qty * px - cost.fee(qty, px))
    return curve
