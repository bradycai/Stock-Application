"""Fail-closed validation of a StrategistProposal against its bundle. Pure: no DB, no network.

Statuses returned: "abstained" (HOLD/WATCH/AVOID or a non-empty abstain_reason, never a trade),
"rejected" (any violation), "accepted" (clean BUY/SELL; position size clamped to the cap).
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.services.recommendations import clamp_position_size
from app.services.research.contracts import StrategistProposal

NON_ACTIONABLE = ("HOLD", "WATCH", "AVOID")
_PRICE_KEYS = ("last_price", "price", "last", "mid", "close")


@dataclass
class BundleView:
    """The slice of a research bundle validation needs."""
    symbol: str
    as_of: datetime
    expires_at: datetime | None
    evidence: dict
    holding: bool = False
    market_snapshot_id: int | None = None


@dataclass
class ValidationResult:
    status: str                                   # accepted | rejected | abstained
    violations: list[dict] = field(default_factory=list)
    position_size_percent: float = 0.0            # clamped
    original_size_percent: float = 0.0
    quote_price: float | None = None
    quote_ts: datetime | None = None

    def as_dict(self) -> dict:
        return {"status": self.status, "violations": self.violations,
                "position_size_percent": self.position_size_percent,
                "original_size_percent": self.original_size_percent,
                "size_clamped": self.position_size_percent < self.original_size_percent,
                "quote_price": self.quote_price,
                "quote_ts": self.quote_ts.isoformat() if self.quote_ts else None}


def _v(code: str, message: str) -> dict:
    return {"code": code, "message": message}


def _utc(t: datetime | None) -> datetime | None:
    if t is not None and t.tzinfo is None:
        return t.replace(tzinfo=timezone.utc)
    return t


def _parse_ts(value) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    if isinstance(value, str):
        try:
            return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def quote_from_evidence(evidence: dict, symbol: str) -> tuple[float | None, datetime | None]:
    """(price, as_of) of the bundle's `quote:<SYMBOL>` evidence item, tolerant of its value shape."""
    item = evidence.get(f"quote:{symbol}")
    if not isinstance(item, dict):
        return None, None
    value = item.get("value")
    price = None
    if isinstance(value, dict):
        for k in _PRICE_KEYS:
            if isinstance(value.get(k), (int, float)) and not isinstance(value.get(k), bool):
                price = float(value[k])
                break
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        price = float(value)
    if price is not None and price <= 0:
        price = None
    return price, _parse_ts(item.get("as_of"))


def validate(p: StrategistProposal, bundle: BundleView, *, now: datetime, max_data_age_sec: float,
             max_limit_deviation_pct: float, max_position_pct: float) -> ValidationResult:
    now = _utc(now)
    price, qts = quote_from_evidence(bundle.evidence, bundle.symbol)
    res = ValidationResult(status="rejected", quote_price=price, quote_ts=qts,
                           original_size_percent=float(p.position_size_percent))

    if p.action in NON_ACTIONABLE or p.abstain_reason.strip():
        res.status = "abstained"
        if p.action in ("BUY", "SELL"):
            res.violations.append(_v("ABSTAIN_REASON_SET",
                                     f"{p.action} carried an abstain_reason; treated as abstention"))
        return res

    v = res.violations
    if p.symbol.strip().upper() != bundle.symbol.upper():
        v.append(_v("SYMBOL_MISMATCH", f"proposal is for {p.symbol!r}, bundle is for {bundle.symbol!r}"))

    ids = [e for e in p.evidence_ids if isinstance(e, str) and e.strip()]
    if not ids:
        v.append(_v("MISSING_EVIDENCE", "a trade must cite at least one evidence id"))
    unknown = sorted({e for e in ids if e not in bundle.evidence})
    if unknown:
        v.append(_v("UNKNOWN_EVIDENCE", f"evidence ids not in the bundle: {', '.join(unknown)}"))
    if not p.thesis.strip():
        v.append(_v("MISSING_THESIS", "a trade needs a stated thesis"))

    # Freshness: the bundle must not be expired and its quote must be recent.
    expires = _utc(bundle.expires_at)
    if expires is not None and expires <= now:
        v.append(_v("BUNDLE_EXPIRED", f"bundle expired at {expires.isoformat()}"))
    ref_ts = qts or _utc(bundle.as_of)
    age = (now - ref_ts).total_seconds()
    if age > max_data_age_sec:
        v.append(_v("STALE_BUNDLE", f"bundle data is {age:.0f}s old (max {max_data_age_sec:.0f}s)"))

    if price is None:
        v.append(_v("NO_QUOTE", f"bundle has no usable quote:{bundle.symbol} evidence"))

    lo, hi, inv, tp = p.entry_low, p.entry_high, p.invalidation_price, p.take_profit_price
    if lo <= 0 or hi <= 0:
        v.append(_v("INVALID_ENTRY", "entry_low and entry_high must be greater than 0"))
    if lo > hi:
        v.append(_v("INVALID_ENTRY_RANGE", f"entry_low {lo} is above entry_high {hi}"))
    if price is not None and lo > 0 and hi > 0:
        for name, val in (("entry_low", lo), ("entry_high", hi)):
            dev = abs(val / price - 1) * 100
            if dev > max_limit_deviation_pct:
                v.append(_v("ENTRY_OFF_MARKET", f"{name} {val:.2f} is {dev:.1f}% from quote "
                                                f"{price:.2f} (max {max_limit_deviation_pct:.0f}%)"))
    if lo > 0 and hi > 0 and lo <= hi:
        if p.action == "BUY":
            if not 0 < inv < lo:
                v.append(_v("INVALID_INVALIDATION", f"BUY invalidation_price {inv} must be below "
                                                    f"the entry range ({lo}-{hi})"))
            if tp <= hi:
                v.append(_v("INVALID_TAKE_PROFIT", f"BUY take_profit_price {tp} must be above "
                                                   f"the entry range ({lo}-{hi})"))
        else:  # SELL
            if inv <= hi:
                v.append(_v("INVALID_INVALIDATION", f"SELL invalidation_price {inv} must be above "
                                                    f"the entry range ({lo}-{hi})"))
            if not 0 < tp < lo:
                v.append(_v("INVALID_TAKE_PROFIT", f"SELL take_profit_price {tp} must be below "
                                                   f"the entry range ({lo}-{hi})"))
    if p.action == "SELL" and not (bundle.holding or f"pos:{bundle.symbol}" in bundle.evidence):
        v.append(_v("NO_POSITION_TO_SELL", f"SELL requires a held {bundle.symbol} position"))

    res.position_size_percent = clamp_position_size(p.position_size_percent, max_position_pct)
    if res.position_size_percent <= 0:
        v.append(_v("ZERO_SIZE", "position_size_percent must be greater than 0 for a trade"))

    res.status = "rejected" if v else "accepted"
    return res
