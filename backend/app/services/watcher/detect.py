"""Deterministic change detection for the market watcher.

`detect()` is pure: it compares one symbol's current observation with the persisted previous
state and returns signals plus the new state. Claude only ever sees symbols that produced at
least one NEW signal (`persist_signals` dedupes on `event_key`).

Observation (dict):
    now: datetime            price: float | None        price_ts: datetime | None
    bars: [{ts, high, low, close, volume}] oldest -> newest (1Min)
    news: [{id: int, headline, is_new: bool}]
    position: {qty, entry_price, stop, thesis, confidence} | None
    regime: str | None       (only supplied for held symbols)
Previous state (dict): initialized, last_price, last_price_ts, last_bar_ts, regime,
    position_fingerprint. A symbol never seen before is only baselined (no signals).
"""
import hashlib
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import SignalEvent


@dataclass(frozen=True)
class DetectConfig:
    volume_spike_mult: float = 3.0       # latest bar volume vs mean of the lookback bars
    volume_min: float = 1000.0           # ignore spikes on tiny volume
    range_lookback_bars: int = 30
    range_min_bars: int = 10
    breakout_margin_pct: float = 0.1     # must exceed the range by this much
    large_move_pct: float = 1.5          # move since the reference price
    reference_max_age_sec: int = 1800    # reference price is refreshed at least this often
    stop_proximity_pct: float = 2.0


@dataclass
class Signal:
    event_type: str
    event_key: str
    occurred_at: datetime
    source: str
    payload: dict
    evidence_ids: list = field(default_factory=list)


@dataclass
class DetectResult:
    signals: list[Signal]
    state: dict


def position_fingerprint(position: dict | None) -> str | None:
    if not position:
        return None
    raw = "|".join(str(position.get(k)) for k in ("qty", "stop", "thesis", "confidence"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _key(symbol: str, kind: str, suffix: str) -> str:
    return f"{symbol}:{kind}:{suffix}"


def _ts(t) -> str:
    return t.isoformat() if isinstance(t, datetime) else str(t)


def detect(symbol: str, obs: dict, prev: dict, cfg: DetectConfig | None = None) -> DetectResult:
    cfg = cfg or DetectConfig()
    now: datetime = obs["now"]
    bars = obs.get("bars") or []
    price = obs.get("price")
    prev = prev or {}
    seeded = bool(prev.get("initialized"))
    signals: list[Signal] = []

    latest = bars[-1] if bars else None
    new_bar = latest is not None and (prev.get("last_bar_ts") is None or latest["ts"] > prev["last_bar_ts"])

    state = {
        "initialized": True,
        "last_price": prev.get("last_price"),
        "last_price_ts": prev.get("last_price_ts"),
        "last_bar_ts": latest["ts"] if latest is not None else prev.get("last_bar_ts"),
        "regime": prev.get("regime") if obs.get("regime") is None else obs["regime"],
        "position_fingerprint": position_fingerprint(obs.get("position")),
    }

    # --- news -------------------------------------------------------------------------
    new_news = [n for n in obs.get("news") or [] if n.get("is_new")]
    if seeded and new_news:
        ids = sorted(n["id"] for n in new_news)
        signals.append(Signal(
            "NEWS", _key(symbol, "NEWS", str(ids[-1])), now, "news",
            {"count": len(ids), "headlines": [n.get("headline") for n in new_news][:5]},
            [f"news:{i}" for i in ids]))

    # --- bar-based: volume spike, breakout / breakdown --------------------------------
    if seeded and new_bar:
        window = bars[-(cfg.range_lookback_bars + 1):-1]
        if len(window) >= cfg.range_min_bars:
            avg_vol = sum(b["volume"] for b in window) / len(window)
            if latest["volume"] >= cfg.volume_min and avg_vol > 0 \
                    and latest["volume"] >= cfg.volume_spike_mult * avg_vol:
                signals.append(Signal(
                    "VOLUME_SPIKE", _key(symbol, "VOLUME_SPIKE", _ts(latest["ts"])), latest["ts"], "bars",
                    {"volume": latest["volume"], "avg_volume": round(avg_vol, 2),
                     "ratio": round(latest["volume"] / avg_vol, 2)}))
            hi = max(b["high"] for b in window)
            lo = min(b["low"] for b in window)
            m = cfg.breakout_margin_pct / 100
            if latest["close"] > hi * (1 + m):
                signals.append(Signal(
                    "BREAKOUT", _key(symbol, "BREAKOUT", _ts(latest["ts"])), latest["ts"], "bars",
                    {"close": latest["close"], "range_high": hi, "lookback_bars": len(window)}))
            elif latest["close"] < lo * (1 - m):
                signals.append(Signal(
                    "BREAKDOWN", _key(symbol, "BREAKDOWN", _ts(latest["ts"])), latest["ts"], "bars",
                    {"close": latest["close"], "range_low": lo, "lookback_bars": len(window)}))

    # --- large move since the reference observation -----------------------------------
    if price is not None:
        ref, ref_ts = prev.get("last_price"), prev.get("last_price_ts")
        moved = False
        if seeded and ref:
            pct = (price / ref - 1) * 100
            if abs(pct) >= cfg.large_move_pct:
                moved = True
                signals.append(Signal(
                    "LARGE_MOVE", _key(symbol, "LARGE_MOVE", f"{_ts(now)[:16]}:{round(price, 2)}"), now,
                    "quote", {"price": price, "reference_price": ref, "change_pct": round(pct, 2)}))
        stale_ref = ref_ts is None or (now - ref_ts).total_seconds() > cfg.reference_max_age_sec
        if ref is None or moved or stale_ref:
            state["last_price"], state["last_price_ts"] = price, now

    # --- position thesis / risk change ------------------------------------------------
    pos = obs.get("position")
    if seeded and pos:
        if state["position_fingerprint"] != prev.get("position_fingerprint"):
            signals.append(Signal(
                "POSITION_CHANGE",
                _key(symbol, "POSITION_CHANGE", state["position_fingerprint"] or "none"), now, "position",
                {"qty": pos.get("qty"), "stop": pos.get("stop"), "confidence": pos.get("confidence"),
                 "thesis_changed": True}))
        stop = pos.get("stop")
        if price is not None and stop and price <= stop * (1 + cfg.stop_proximity_pct / 100):
            signals.append(Signal(
                "STOP_PROXIMITY", _key(symbol, "STOP_PROXIMITY", f"{stop}:{_ts(now)[:13]}"), now, "position",
                {"price": price, "stop": stop}))

    # --- market regime change (supplied only for held symbols) -------------------------
    regime = obs.get("regime")
    if seeded and regime and prev.get("regime") and regime != prev["regime"]:
        signals.append(Signal(
            "REGIME_CHANGE", _key(symbol, "REGIME_CHANGE", f"{prev['regime']}>{regime}:{_ts(now)[:10]}"),
            now, "market_context", {"from": prev["regime"], "to": regime}))

    return DetectResult(signals, state)


def persist_signals(db: Session, symbol: str, signals: list[Signal], run_id: str | None = None) -> list[dict]:
    """Insert signals into `signal_events`, skipping ones already seen (same event_key).
    Returns only the NEWLY inserted ones as {id, event_key, ...} dicts. Caller commits."""
    fresh = []
    for s in signals:
        stmt = pg_insert(SignalEvent).values(
            event_key=s.event_key, symbol=symbol, event_type=s.event_type, source=s.source,
            occurred_at=s.occurred_at, run_id=run_id,
            payload={**s.payload, "evidence_ids": s.evidence_ids},
        ).on_conflict_do_nothing(index_elements=["event_key"]).returning(SignalEvent.id)
        new_id = db.execute(stmt).scalar_one_or_none()
        if new_id is not None:
            fresh.append({"id": new_id, "event_key": s.event_key, "event_type": s.event_type,
                          "occurred_at": s.occurred_at.isoformat(), "payload": s.payload,
                          "evidence_ids": s.evidence_ids})
    return fresh
