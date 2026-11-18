"""Pure fill and cost model for daily bars.

Orders are decided at a close and can only fill on a LATER bar, so no fill price depends on
information that was unknowable at decision time. Intrabar ordering is unknown, so whenever a
stop and a target could both trigger in the same bar the stop is assumed.
"""
import math
from dataclasses import dataclass

from app.backtest.data import Bar


@dataclass(frozen=True)
class CostModel:
    slippage_bps: float = 5.0
    fee_bps: float = 0.0
    fee_per_share: float = 0.0
    max_volume_pct: float = 10.0   # an order can take at most this % of the bar's volume

    def fee(self, qty: int, price: float) -> float:
        return qty * price * self.fee_bps / 10_000 + qty * self.fee_per_share

    def volume_cap(self, bar: Bar) -> int:
        return max(0, math.floor(bar.volume * self.max_volume_pct / 100))


@dataclass(frozen=True)
class Fill:
    qty: int
    price: float
    fee: float
    reason: str = ""

    @property
    def notional(self) -> float:
        return self.qty * self.price


def _make(qty: int, price: float, cost: CostModel, bar: Bar, reason: str = "") -> Fill | None:
    qty = min(qty, cost.volume_cap(bar))
    if qty < 1:
        return None
    return Fill(qty, price, cost.fee(qty, price), reason)


def fill_buy_limit(limit: float, qty: int, bar: Bar, cost: CostModel) -> Fill | None:
    """Day limit buy resting on `bar`: fills if low <= limit, at min(open, limit) + slippage,
    never above the limit (or the bar high)."""
    if qty < 1 or bar.low > limit:
        return None
    price = min(min(bar.open, limit) * (1 + cost.slippage_bps / 10_000), limit, bar.high)
    return _make(qty, price, cost, bar, "entry")


def fill_sell_limit(limit: float, qty: int, bar: Bar, cost: CostModel, reason: str = "limit_sell"
                    ) -> Fill | None:
    """Symmetric to the limit buy: fills if high >= limit, at max(open, limit) - slippage,
    never below the limit (or the bar low)."""
    if qty < 1 or bar.high < limit:
        return None
    price = max(max(bar.open, limit) * (1 - cost.slippage_bps / 10_000), limit, bar.low)
    return _make(qty, price, cost, bar, reason)


def fill_market_at_open(side: str, qty: int, bar: Bar, cost: CostModel, reason: str) -> Fill | None:
    sign = 1 if side == "buy" else -1
    price = bar.open * (1 + sign * cost.slippage_bps / 10_000)
    return _make(qty, min(max(price, bar.low), bar.high), cost, bar, reason)


def fill_market_at_close(side: str, qty: int, bar: Bar, cost: CostModel, reason: str) -> Fill | None:
    sign = 1 if side == "buy" else -1
    price = bar.close * (1 + sign * cost.slippage_bps / 10_000)
    return _make(qty, price, cost, bar, reason)


def evaluate_exit(bar: Bar, stop: float, target: float | None, qty: int, cost: CostModel,
                  allow_target: bool = True) -> Fill | None:
    """Stop-loss / take-profit for a long position during `bar`.

    Stop is checked first: if both could trigger in one bar the stop wins. A gap through the
    stop fills at the (worse) open; a gap above the target fills at the (better) open.
    A triggered stop is a market order (slippage, bounded by the bar low); a target is a limit
    order (never below the target). `allow_target=False` on the entry bar, where the order of
    the entry and the high is unknowable.
    """
    if bar.low <= stop:
        price = max(min(bar.open, stop) * (1 - cost.slippage_bps / 10_000), bar.low)
        return _make(qty, price, cost, bar, "stop_loss")
    if allow_target and target is not None and bar.high >= target:
        return fill_sell_limit(target, qty, bar, cost, "take_profit")
    return None
