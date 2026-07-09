"""Pure logic for AI-managed exits: trailing-stop sizing/advancement, partial-profit
ladder, and position-level review triggers. No DB/broker/LLM — fully unit-testable. The
monitor wires these to live state and the broker.
"""
ATR_MULTIPLIER = 2.5
MIN_TRAIL_PCT = 3.0
MAX_TRAIL_PCT = 12.0


def trailing_stop_distance_pct(atr: float | None, price: float, fallback_pct: float) -> float:
    """Trailing distance as a percent of price. Uses ATR (volatility) when available,
    clamped to a sane band; otherwise the fixed fallback (e.g. 6%)."""
    if atr and price and price > 0:
        pct = (atr * ATR_MULTIPLIER) / price * 100
        return round(max(MIN_TRAIL_PCT, min(MAX_TRAIL_PCT, pct)), 2)
    return float(fallback_pct)


def update_trailing_stop(current_stop: float | None, highest_price: float, distance_pct: float) -> float:
    """Recompute the trailing stop from the highest price seen. The stop can ONLY move up,
    never down."""
    candidate = round(highest_price * (1 - distance_pct / 100), 2)
    if current_stop is None:
        return candidate
    return max(float(current_stop), candidate)


def gain_pct(entry_price: float, current_price: float) -> float:
    if not entry_price:
        return 0.0
    return (current_price / entry_price - 1) * 100


def next_partial_level(current_gain_pct: float, levels: list[float], taken: list[float]):
    """Return the highest not-yet-taken partial-profit level the gain has reached, or None.
    (Highest so a gap-up past several levels takes the most-advanced one first.)"""
    eligible = [lv for lv in levels if current_gain_pct >= lv and lv not in taken]
    return max(eligible) if eligible else None


def partial_qty(original_qty: int, amount_pct: float) -> int:
    """Shares to sell for a partial = amount_pct of the ORIGINAL position (>=1)."""
    return max(1, int(round(original_qty * amount_pct / 100)))


def is_trailing_stop_hit(current_price: float, trailing_stop: float | None) -> bool:
    return trailing_stop is not None and current_price <= float(trailing_stop)


def near_trailing_stop(current_price: float, trailing_stop: float | None, proximity_pct: float = 1.5) -> bool:
    """True when price is within `proximity_pct` ABOVE the trailing stop (approaching it)."""
    if trailing_stop is None or current_price <= 0:
        return False
    ts = float(trailing_stop)
    return ts < current_price <= ts * (1 + proximity_pct / 100)


def decide_exit(action: str, confidence_pct: float, ai_exit_confidence: float) -> str:
    """Map a Claude position decision to an exit outcome. Pure → unit-tested.
    Returns 'sell' | 'trim' | 'tighten_stop' | 'hold'.
    - SELL only exits when confidence >= AI_EXIT_CONFIDENCE (else hold).
    - TRIM always trims (risk-reducing). HOLD/ADD => hold (no exit)."""
    if action == "sell":
        return "sell" if confidence_pct >= ai_exit_confidence else "hold"
    if action == "trim":
        return "trim"
    if action == "tighten_stop":
        return "tighten_stop"
    return "hold"


def position_review_triggers(
    *,
    price: float,
    prev_price: float | None,
    pnl_pct: float | None,
    prev_pnl_pct: float | None,
    highest_price: float,
    new_high: bool,
    trailing_stop: float | None,
    seconds_since_review: float | None,
    recheck_interval: float,
    hold_days: float | None,
    max_hold_days: float,
) -> list[str]:
    """Reasons to re-ask Claude about a HELD position. Empty => no Claude call this cycle."""
    out = []
    if prev_price and price and abs(price / prev_price - 1) * 100 >= 2:
        out.append("PRICE_MOVE")
    if pnl_pct is not None and prev_pnl_pct is not None and abs(pnl_pct - prev_pnl_pct) >= 3:
        out.append("PNL_MOVE")
    if new_high:
        out.append("NEW_HIGH")
    if near_trailing_stop(price, trailing_stop):
        out.append("NEAR_TRAILING_STOP")
    if seconds_since_review is None or seconds_since_review >= recheck_interval:
        out.append("CONFIDENCE_DECAY")
    if hold_days is not None and hold_days >= max_hold_days:
        out.append("MAX_HOLD")
    return out
