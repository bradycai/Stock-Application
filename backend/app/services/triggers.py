"""Rule-based trigger detection for the continuous monitor.

Pure functions: given the previous snapshot and the current snapshot of a ticker, return
the list of triggers that fired. This is the cheap gate that runs every cycle so we only
call Claude when something actually changed.

A "snapshot" is a plain dict with keys (any may be None):
    price, pnl_pct, rel_volume, rsi, macd_hist, news_count, stop_price, take_price
`pnl_pct` is the position's unrealized P&L percent (None for non-positions).
"""
from dataclasses import dataclass

# Thresholds (mirror the spec).
PRICE_MOVE_PCT = 2.0        # ±2% from last check
PNL_MOVE_PCT = 3.0          # ±3% position P&L change from last check
VOLUME_SPIKE_X = 2.0        # 2x average volume
RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0
ZONE_PROXIMITY_PCT = 1.5    # within 1.5% of stop/take = "zone approached"


@dataclass
class Trigger:
    type: str
    detail: str

    def as_dict(self) -> dict:
        return {"type": self.type, "detail": self.detail}


def _g(d, k):
    return (d or {}).get(k)


def detect_triggers(prev: dict | None, snap: dict) -> list[Trigger]:
    """Return triggers comparing the current snapshot `snap` to the previous `prev`.

    On the first observation (prev is None) we seed state and fire nothing.
    """
    out: list[Trigger] = []

    price = _g(snap, "price")
    rel_vol = _g(snap, "rel_volume")
    rsi = _g(snap, "rsi")
    macd = _g(snap, "macd_hist")
    news = _g(snap, "news_count") or 0
    stop = _g(snap, "stop_price")
    take = _g(snap, "take_price")

    # Volume spike and stop/take zones don't need history — check them always.
    if rel_vol is not None and rel_vol >= VOLUME_SPIKE_X:
        out.append(Trigger("VOLUME_SPIKE", f"relative volume {rel_vol:.1f}x"))

    if stop and price and stop < price <= stop * (1 + ZONE_PROXIMITY_PCT / 100):
        out.append(Trigger("STOP_ZONE", f"price {price} within {ZONE_PROXIMITY_PCT}% of stop {stop}"))

    if take and price and take * (1 - ZONE_PROXIMITY_PCT / 100) <= price < take:
        out.append(Trigger("TAKE_ZONE", f"price {price} within {ZONE_PROXIMITY_PCT}% of take {take}"))

    if prev is None:
        return out  # first observation — seed, only the stateless triggers above

    # Price move since last check
    pprice = _g(prev, "price")
    if pprice and price:
        chg = (price / pprice - 1) * 100
        if abs(chg) >= PRICE_MOVE_PCT:
            out.append(Trigger("PRICE_MOVE", f"{chg:+.2f}% since last check"))

    # Position P&L move since last check
    ppnl, pnl = _g(prev, "pnl_pct"), _g(snap, "pnl_pct")
    if ppnl is not None and pnl is not None and abs(pnl - ppnl) >= PNL_MOVE_PCT:
        out.append(Trigger("PNL_MOVE", f"P&L moved {pnl - ppnl:+.2f}pts to {pnl:.2f}%"))

    # RSI crossing overbought / oversold
    prsi = _g(prev, "rsi")
    if prsi is not None and rsi is not None:
        if prsi < RSI_OVERBOUGHT <= rsi:
            out.append(Trigger("RSI_CROSS", f"RSI crossed overbought ({rsi:.0f})"))
        elif prsi > RSI_OVERSOLD >= rsi:
            out.append(Trigger("RSI_CROSS", f"RSI crossed oversold ({rsi:.0f})"))

    # MACD histogram sign flip = trend reversal
    pmacd = _g(prev, "macd_hist")
    if pmacd is not None and macd is not None:
        if (pmacd <= 0 < macd) or (pmacd >= 0 > macd):
            direction = "bullish" if macd > 0 else "bearish"
            out.append(Trigger("MACD_REVERSAL", f"MACD histogram flipped {direction}"))

    # New headline(s) since last check
    pnews = _g(prev, "news_count") or 0
    if news > pnews:
        out.append(Trigger("NEWS", f"{news - pnews} new headline(s)"))

    return out
