"""Adaptive risk policies. Pure and deterministic (no DB, network or clock).

These sit AFTER the hard rules in risk.py and can only make a BUY smaller or reject it:
the returned size is never larger than the incoming size or MAX_POSITION_PCT, and the
caller must re-run risk.evaluate on the reduced size (see `apply`). Sells pass through.

Policies (all thresholds come from settings):
  RISK_BUDGET           stop% x size% <= RISK_PER_TRADE_PCT of equity; high ATR% shrinks size
  EXPOSURE_LIMIT        total gross exposure cap
  SECTOR_CONCENTRATION  per-sector cap (unknown sector = its own bucket)
  CORRELATED_HOLDINGS   too many holdings highly correlated with the candidate
  ILLIQUID              minimum average daily dollar volume, and position <= % of ADV
  MARKET_CLOSED         optional: no new entries while the market is closed

A size reduction is a note. A violation means the entry is rejected (also used when a
reduction would shrink the position below POLICY_MIN_SIZE_PCT).
"""
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from app.config import settings as default_settings
from app.services import risk


@dataclass
class PolicyResult:
    violations: list[dict] = field(default_factory=list)
    size_pct: float = 0.0
    notes: list[str] = field(default_factory=list)


def _f(value, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _num(value) -> float | None:
    """A finite float, or None when missing/malformed."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _floor4(x: float) -> float:
    return math.floor(x * 10_000) / 10_000


def correlation(a: Sequence, b: Sequence, min_obs: int) -> float | None:
    """Pearson correlation over the aligned tails of two return series (None entries are
    skipped pairwise). None when there are too few observations or no variance."""
    n = min(len(a), len(b))
    pairs = [(x, y) for x, y in zip(list(a)[-n:], list(b)[-n:]) if x is not None and y is not None]
    if n == 0 or len(pairs) < max(2, min_obs):
        return None
    mx = sum(x for x, _ in pairs) / len(pairs)
    my = sum(y for _, y in pairs) / len(pairs)
    sxx = sum((x - mx) ** 2 for x, _ in pairs)
    syy = sum((y - my) ** 2 for _, y in pairs)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in pairs)
    return sxy / math.sqrt(sxx * syy)


def evaluate_policies(
    intent: dict,
    *,
    account: Mapping,
    positions: Sequence[Mapping],
    market: Mapping | None,
    sector_of: Callable[[str], str | None],
    returns_by_symbol: Mapping[str, Sequence] | None,
    settings=default_settings,
) -> PolicyResult:
    """Evaluate the policies for a BUY intent. `market` carries last_price, atr_14 (price
    units), avg_volume_20 (shares) and market_open (True/False/None). `positions` holds
    market_value (or qty x current_price); callers append pending buys / open buy orders as
    extra entries. `returns_by_symbol=None` means the bar query failed (fail closed when
    there are holdings to compare against)."""
    size_in = _f(intent.get("position_size_percent"))
    if intent.get("action") != "buy":
        return PolicyResult(size_pct=size_in)

    market = market or {}
    violations: list[dict] = []
    notes: list[str] = []
    size = min(size_in, _f(settings.max_position_pct))
    min_size = _f(settings.policy_min_size_pct)
    ticker = intent.get("ticker")
    equity = _f(account.get("equity"))

    def violate(code: str, message: str) -> None:
        violations.append({"code": code, "message": message})

    def cap(limit_size: float, code: str, label: str) -> None:
        """Reduce `size` to `limit_size`, or reject when the cut would leave a sliver."""
        nonlocal size
        limit_size = _floor4(max(limit_size, 0.0))
        if limit_size >= size:
            return
        if limit_size < min_size:
            violate(code, f"{label}: allowed size {limit_size}% is below the {min_size}% minimum")
        else:
            notes.append(f"{code}: size reduced {size}% -> {limit_size}% ({label})")
            size = limit_size

    if size <= 0:
        return PolicyResult(size_pct=max(size, 0.0), notes=notes)
    if equity <= 0:
        violate("EXPOSURE_LIMIT", "Account equity is not positive; cannot size a position")
        return PolicyResult(violations=violations, size_pct=size, notes=notes)

    # Market status (optional). Unknown state counts as closed (fail closed).
    if settings.block_when_market_closed and market.get("market_open") is not True:
        violate("MARKET_CLOSED", "Market is closed (or its state is unknown); new entries are blocked")

    # Risk budget: loss at the stop must stay within RISK_PER_TRADE_PCT of equity.
    stop_pct = _f(intent.get("stop_loss_percent"))
    if stop_pct > 0:  # a missing stop is the hard rule's job (NO_STOP_LOSS)
        cap(_f(settings.risk_per_trade_pct) * 100.0 / stop_pct, "RISK_BUDGET",
            f"stop {stop_pct}% x size must risk <= {settings.risk_per_trade_pct}% of equity")
    price = _f(market.get("last_price")) or _f(intent.get("limit_price"))
    atr = _f(market.get("atr_14"))
    atr_high = _f(settings.atr_high_pct)
    if not atr > 0:
        notes.append("RISK_BUDGET: volatility unknown (ATR unavailable); no volatility adjustment")
    if atr > 0 and price > 0 and atr_high > 0:
        atr_pct = atr / price * 100.0
        if atr_pct > atr_high:
            cap(size * atr_high / atr_pct, "RISK_BUDGET",
                f"volatility ATR {atr_pct:.1f}% of price exceeds {atr_high}%")

    # Portfolio limits (exposure counted at current market value, % of equity).
    holdings: list[tuple[str, float]] = []
    unknown_exposure: list[str] = []
    for p in positions:
        sym = p.get("symbol")
        mv = _num(p.get("market_value"))
        qty, cur = _num(p.get("qty")), _num(p.get("current_price"))
        if mv is None or (mv == 0 and qty):  # missing, or a suspicious zero for a held quantity
            mv = qty * cur if qty is not None and cur is not None and cur > 0 else None
        if mv is None:
            unknown_exposure.append(str(sym))
        else:
            holdings.append((sym, abs(mv)))
    if unknown_exposure:
        violate("EXPOSURE_UNKNOWN", "Exposure of " + ", ".join(unknown_exposure)
                + " cannot be determined (no market value or qty x price)")
    held_symbols = list(dict.fromkeys(sym for sym, _ in holdings))
    gross_pct = sum(v for _, v in holdings) / equity * 100.0
    cap(_f(settings.max_gross_exposure_pct) - gross_pct, "EXPOSURE_LIMIT",
        f"gross exposure {gross_pct:.1f}% of max {settings.max_gross_exposure_pct}%")

    # A lookup that RAISES is an error (fail closed); None is a genuinely unknown sector.
    try:
        sectors = {sym: sector_of(sym) for sym in dict.fromkeys([ticker, *held_symbols])}
    except Exception as e:  # noqa: BLE001
        sectors = None
        violate("SECTOR_UNKNOWN", f"Sector lookup failed ({type(e).__name__}); cannot check concentration")
    if sectors is not None:
        def bucket(sym: str) -> str:
            return str(sectors[sym]) if sectors[sym] else f"UNKNOWN:{sym}"

        mine = bucket(ticker)
        sector_pct = sum(v for s, v in holdings if bucket(s) == mine) / equity * 100.0
        cap(_f(settings.max_sector_exposure_pct) - sector_pct, "SECTOR_CONCENTRATION",
            f"sector '{mine}' exposure {sector_pct:.1f}% of max {settings.max_sector_exposure_pct}%")

    # Correlation must not fail open: with holdings to compare against, missing candidate
    # history (or a failed bar query) rejects; holdings lacking history are noted.
    others = [s for s in held_symbols if s != ticker]
    if others:
        min_obs = int(settings.correlation_min_obs)
        candidate = (returns_by_symbol or {}).get(ticker) or []
        if returns_by_symbol is None:
            violate("CORRELATION_UNKNOWN", "Daily bars unavailable; cannot check correlation with holdings")
        elif sum(1 for r in candidate if r is not None) < min_obs:
            violate("CORRELATION_UNKNOWN",
                    f"{ticker} has fewer than {min_obs} daily returns; cannot check correlation")
        else:
            threshold = _f(settings.correlation_threshold)
            corr_hits = []
            for sym in others:
                c = correlation(candidate, returns_by_symbol.get(sym) or [], min_obs)
                if c is None:
                    notes.append(f"CORRELATION: {sym} has insufficient history; correlation not checked")
                elif c > threshold:
                    corr_hits.append(f"{sym} ({c:.2f})")
            if len(corr_hits) >= int(settings.max_correlated_holdings):
                violate("CORRELATED_HOLDINGS",
                        f"{ticker} is correlated above {threshold} with {len(corr_hits)} holdings: "
                        + ", ".join(corr_hits))

    # Liquidity. Unknown average volume fails closed.
    adv_usd = _f(market.get("avg_volume_20")) * price
    if adv_usd <= 0:
        violate("ILLIQUID", "Average daily dollar volume is unknown; cannot verify liquidity")
    elif adv_usd < _f(settings.min_adv_usd):
        violate("ILLIQUID", f"Average daily dollar volume ${adv_usd:,.0f} is below the "
                            f"${_f(settings.min_adv_usd):,.0f} minimum")
    else:
        max_notional = adv_usd * _f(settings.max_pct_of_adv) / 100.0
        cap(max_notional / equity * 100.0, "ILLIQUID",
            f"position must be <= {settings.max_pct_of_adv}% of ${adv_usd:,.0f} ADV")

    return PolicyResult(violations=violations, size_pct=min(size, size_in), notes=notes)


def apply(intent: dict, ri: "risk.RiskInput", result: "risk.RiskResult", *,
          positions: Sequence[Mapping], market: Mapping | None,
          sector_of: Callable[[str], str | None], returns_by_symbol: Mapping[str, Sequence] | None,
          ) -> tuple["risk.RiskResult", list[str]]:
    """Layer the policies on a risk.evaluate result. Returns (result, notes).

    Only BUYs that already passed risk are examined (a failed intent cannot be loosened).
    On a policy violation the result is failed with the extra violations. On a size cut,
    `intent["position_size_percent"]` is lowered in place and risk.evaluate is re-run on it,
    so the quantity always comes from the hard-rule engine."""
    if intent.get("action") != "buy" or not result.passed:
        return result, []
    pr = evaluate_policies(intent, account={"equity": ri.equity}, positions=positions, market=market,
                           sector_of=sector_of, returns_by_symbol=returns_by_symbol)
    if pr.violations:
        extra = [risk.Violation(v["code"], v["message"]) for v in pr.violations]
        return risk.RiskResult(passed=False, violations=list(result.violations) + extra), pr.notes
    if pr.size_pct < _f(intent.get("position_size_percent")):
        intent["position_size_percent"] = pr.size_pct
        ri.intent = intent
        result = risk.evaluate(ri)
    return result, pr.notes


def fail_closed(result: "risk.RiskResult", exc: Exception) -> tuple["risk.RiskResult", list[str]]:
    """Result for an unexpected policy error: the intent is rejected with RISK_POLICY_ERROR."""
    msg = f"Risk policies could not be evaluated ({type(exc).__name__}); failing closed"
    return (risk.RiskResult(passed=False, violations=list(result.violations)
                            + [risk.Violation("RISK_POLICY_ERROR", msg)]), [])
