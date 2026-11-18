"""Replay recorded AI strategist proposals / intents against daily bars. Offline and read-only.

No Claude, no broker, no network: outputs come from `ai_cache.RecordedOutputs` and a cache miss
is a fail-closed skip (recorded as `cache_miss`). Each recorded decision is

  1. fetched from the cache and re-validated with the real `strategist.validate`, with
     `now` = the decision timestamp (so stale bundles / quotes are rejected exactly as live),
  2. sized and re-checked by the pure `risk.evaluate` against a simulated account,
  3. simulated with the existing fill model (`fills.py`) on bars STRICTLY AFTER the decision date
     (`BarStore.bars_after`): limit entry (spread/slippage, partial fills, fees), the proposal's
     invalidation (stop), take-profit and holding horizon. One position per decision.

Limitations are documented in the report `notes`. Results are grouped by
(strategy_id, model, prompt_version) and compared with the momentum baseline and SPY buy-and-hold
over the same dates, per chronological window.
"""
import bisect
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from statistics import mean

from pydantic import ValidationError

from app.backtest import ai_cache
from app.backtest.data import Bar, BarStore
from app.backtest.engine import BacktestConfig, Trade, buy_and_hold
from app.backtest.fills import (
    CostModel,
    Fill,
    evaluate_exit,
    fill_buy_limit,
    fill_market_at_close,
    fill_market_at_open,
)
from app.backtest.metrics import compute_metrics
from app.backtest.runner import run_window
from app.backtest.splits import chronological_split, slice_dates, walk_forward_windows
from app.config import settings
from app.services.recommendations import clamp_position_size
from app.services.research.contracts import StrategistProposal
from app.services.risk import RiskInput, evaluate
from app.services.strategist import STRATEGY_ID, validate

CALIBRATION_BINS = (0.0, 0.5, 0.6, 0.7, 0.8, 0.9, 1.000001)
NOTES = [
    "Entries fill only on bars strictly after the decision date (day limit order, first bar).",
    "Daily bars: intrabar ordering unknown, so a stop wins a same-bar tie; no target on the entry bar.",
    "Each decision is one independent position; cash/positions are checked via risk.evaluate against "
    "initial cash plus P&L realized before the decision date.",
    "SELL proposals are not simulated (the replay is long-only).",
    "Trades exiting after a window end are marked at the window's last close in that window's curve.",
]


@dataclass(frozen=True)
class ReplayConfig:
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    default_horizon_days: int = 20
    min_samples: int = 30
    min_bucket_samples: int = 5
    max_entry_gap_days: int = 7
    max_data_age_sec: float = float(settings.max_data_age_sec)
    max_limit_deviation_pct: float = float(settings.max_limit_deviation_pct)
    max_position_pct: float = float(settings.max_position_pct)

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "backtest"}
        return {**d, "backtest": self.backtest.to_dict()}


@dataclass
class Candidate:
    """One recorded decision to replay (loaded from the DB or built in tests)."""
    id: int
    source: str                      # proposal | intent
    ref_id: int                      # cache ref: bundle id (proposal) or intent id
    symbol: str
    strategy_id: str
    model: str | None
    prompt_version: str | None
    decision_ts: datetime
    bundle: validate.BundleView | None = None

    @property
    def decision_date(self) -> date:
        return _utc(self.decision_ts).astimezone(timezone.utc).date()

    @property
    def group(self) -> str:
        return f"{self.strategy_id}|{self.model or ''}|{self.prompt_version or ''}"


@dataclass(frozen=True)
class Decision:
    limit: float
    stop: float
    target: float | None
    horizon_days: int
    size_pct: float
    confidence: float | None
    ref_price: float
    missing_fundamentals: bool = False


@dataclass
class SimTrade:
    trade: Trade
    group: str
    candidate_id: int
    decision_date: date
    confidence: float | None
    entry_fee: float
    entry_notional: float
    exit_notional: float


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _dec(x: float) -> Decimal:
    return Decimal(str(round(x, 6)))


# --------------------------------------------------------------------------- loading
def load_candidates(db, start: date, end: date) -> list[Candidate]:
    """Recorded proposals (+ AI intents not already produced by a proposal) decided in [start, end]."""
    from datetime import timedelta

    from sqlalchemy import select

    from app.models import ResearchBundle, StrategyProposal, TradeIntent

    lo = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
    hi = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    out: list[Candidate] = []
    linked: set[int] = set()
    for p in db.scalars(select(StrategyProposal).where(StrategyProposal.created_at >= lo,
                                                       StrategyProposal.created_at < hi)
                        .order_by(StrategyProposal.id)):
        if p.intent_id:
            linked.add(p.intent_id)
        b = db.get(ResearchBundle, p.bundle_id)
        view = None
        if b is not None:
            view = validate.BundleView(
                symbol=b.symbol, as_of=validate._utc(b.as_of), expires_at=b.expires_at,
                evidence=b.evidence or {}, holding=bool(b.holding), market_snapshot_id=b.market_snapshot_id)
        out.append(Candidate(p.id, "proposal", p.bundle_id, p.symbol, p.strategy_id, p.model,
                             p.strategy_version, _utc(p.created_at), view))
    from app.models import AnalysisRun
    rows = db.execute(select(TradeIntent, AnalysisRun.model)
                      .join(AnalysisRun, AnalysisRun.id == TradeIntent.run_id)
                      .where(TradeIntent.strategy_id.is_not(None), TradeIntent.created_at >= lo,
                             TradeIntent.created_at < hi).order_by(TradeIntent.id))
    for it, model in rows:
        if it.id not in linked:
            out.append(Candidate(it.id, "intent", it.id, it.ticker, it.strategy_id, model,
                                 it.prompt_version, _utc(it.created_at)))
    return out


# --------------------------------------------------------------------------- decide (pure)
def decide(cand: Candidate, recorded: ai_cache.RecordedOutputs, cfg: ReplayConfig
           ) -> tuple[Decision | None, str, list[str]]:
    """(decision, status, reasons). status: accepted | cache_miss | invalid_recorded_output |
    rejected | abstained | skipped. Never reaches a model: only `recorded` is consulted."""
    try:
        if cand.source == "proposal":
            call = recorded.strategist_call_fn(model=cand.model, prompt_version=cand.prompt_version,
                                               bundle_id=cand.ref_id, output_model=StrategistProposal)
            p: StrategistProposal = call("", "").output
        else:
            raw = recorded.get(ai_cache.INTENT, cand.model, cand.prompt_version, cand.ref_id)
    except ai_cache.CacheMiss as e:
        return None, "cache_miss", [e.key.label()]
    except ValidationError as e:
        return None, "invalid_recorded_output", [f"{len(e.errors())} validation error(s)"]
    if cand.source == "intent":
        return _decide_intent(raw, cfg)
    if cand.bundle is None:
        return None, "skipped", ["missing_bundle"]
    vres = validate.validate(p, cand.bundle, now=cand.decision_ts, max_data_age_sec=cfg.max_data_age_sec,
                             max_limit_deviation_pct=cfg.max_limit_deviation_pct,
                             max_position_pct=cfg.max_position_pct)
    if vres.status == "abstained":
        return None, "abstained", [v["code"] for v in vres.violations]
    if vres.status != "accepted":
        return None, "rejected", [v["code"] for v in vres.violations]
    if p.action != "BUY":
        return None, "skipped", ["sell_not_simulated"]
    ev = cand.bundle.evidence
    missing_fund = not any(str(k).startswith("fund:") for k in ev)
    horizon = p.holding_horizon_days or cfg.default_horizon_days
    return (Decision(limit=round((p.entry_low + p.entry_high) / 2, 4), stop=p.invalidation_price,
                     target=p.take_profit_price or None, horizon_days=horizon,
                     size_pct=vres.position_size_percent, confidence=p.confidence,
                     ref_price=vres.quote_price or 0.0, missing_fundamentals=missing_fund),
            "accepted", [])


def _decide_intent(raw: dict, cfg: ReplayConfig) -> tuple[Decision | None, str, list[str]]:
    if str(raw.get("action")) != "buy":
        return None, "skipped", ["sell_not_simulated" if raw.get("action") == "sell" else "not_a_buy"]
    limit, stop_pct = raw.get("limit_price"), raw.get("stop_loss_percent")
    size = raw.get("position_size_percent")
    if not limit or limit <= 0 or not stop_pct or stop_pct <= 0 or size is None:
        return None, "rejected", ["INCOMPLETE_INTENT"]
    take = raw.get("take_profit_percent")
    return (Decision(limit=float(limit), stop=float(limit) * (1 - stop_pct / 100),
                     target=float(limit) * (1 + take / 100) if take else None,
                     horizon_days=cfg.default_horizon_days,
                     size_pct=clamp_position_size(size, cfg.max_position_pct),
                     confidence=raw.get("confidence"), ref_price=float(limit)), "accepted", [])


# --------------------------------------------------------------------------- simulate (pure)
def simulate_position(symbol: str, dec: Decision, qty: int, cash: float, bars: list[Bar],
                      cost: CostModel) -> tuple[dict | None, str]:
    """Entry on bars[0], then exits. `bars` must already be strictly after the decision."""
    entry_bar = bars[0]
    fill = fill_buy_limit(dec.limit, qty, entry_bar, cost)
    if fill is None:
        return None, "no_fill"
    q = fill.qty
    while q > 0 and q * fill.price + cost.fee(q, fill.price) > cash:
        q -= 1
    if q < 1:
        return None, "no_fill"
    entry_fee = cost.fee(q, fill.price)
    basis = q * fill.price + entry_fee
    rem, proceeds, fees = q, 0.0, entry_fee
    exit_qty, exit_notional, reason, exit_date = 0, 0.0, "", entry_bar.date
    days_held, time_exit = 0, False

    def sell(f: Fill, d: date) -> None:
        nonlocal rem, proceeds, fees, exit_qty, exit_notional, reason, exit_date
        rem -= f.qty
        proceeds += f.notional - f.fee
        fees += f.fee
        exit_qty += f.qty
        exit_notional += f.notional
        reason, exit_date = f.reason, d

    for i, bar in enumerate(bars):
        if time_exit:
            f = fill_market_at_open("sell", rem, bar, cost, "horizon")
            if f:
                sell(f, bar.date)
        if rem > 0:
            f = evaluate_exit(bar, dec.stop, dec.target, rem, cost, allow_target=i != 0)
            if f:
                sell(f, bar.date)
        if rem == 0:
            break
        days_held += 1
        if days_held >= dec.horizon_days:
            time_exit = True
    if rem > 0:  # end of data: liquidate at the last close (forced if the volume cap blocks it)
        last = bars[-1]
        f = fill_market_at_close("sell", rem, last, cost, "end_of_data")
        if f is None:
            f = Fill(rem, last.close, cost.fee(rem, last.close), "end_of_data")
        sell(f, last.date)
    pnl = proceeds - basis
    return ({"qty": q, "entry_date": entry_bar.date, "exit_date": exit_date, "entry_price": fill.price,
             "exit_price": exit_notional / exit_qty, "reason": reason, "pnl": pnl,
             "return_pct": pnl / basis * 100, "fees": fees, "days_held": days_held, "entry_fee": entry_fee,
             "entry_notional": q * fill.price, "exit_notional": exit_notional}, "filled")


def _risk(cand: Candidate, dec: Decision, equity: float, cash: float, open_positions: int,
          held: set[str]):
    stop_pct = (dec.limit - dec.stop) / dec.limit * 100
    intent = {"action": "buy", "ticker": cand.symbol.upper(), "order_type": "limit",
              "limit_price": str(round(dec.limit, 4)), "stop_loss_percent": str(round(stop_pct, 4)),
              "position_size_percent": str(dec.size_pct)}
    return evaluate(RiskInput(
        intent=intent, equity=_dec(equity), cash=_dec(cash), open_positions=open_positions,
        held_symbols=held, day_pnl_pct=Decimal(0), last_price=_dec(dec.ref_price or dec.limit),
        intraday_change_pct=Decimal(0), is_margin_account=False, is_option=False, trading_halted=False))


def replay_decisions(store: BarStore, candidates: list[Candidate], recorded: ai_cache.RecordedOutputs,
                     cfg: ReplayConfig) -> tuple[list[SimTrade], list[dict]]:
    """Run every candidate; returns (simulated trades, one record per candidate)."""
    cost = cfg.backtest.cost_model()
    initial = cfg.backtest.initial_cash
    records: list[dict] = []
    done: dict[str, list[SimTrade]] = {}
    seen: set[tuple[str, int]] = set()
    for cand in sorted(candidates, key=lambda c: (c.decision_ts, c.source, c.id)):
        rec = {"id": cand.id, "source": cand.source, "symbol": cand.symbol.upper(), "group": cand.group,
               "decision_ts": _utc(cand.decision_ts).isoformat(), "status": "skipped", "reasons": []}
        records.append(rec)
        key = (cand.source, cand.ref_id)
        if key in seen:  # one simulated position per recorded decision
            rec["reasons"] = ["duplicate_decision"]
            continue
        seen.add(key)
        dec, status, reasons = decide(cand, recorded, cfg)
        rec["status"], rec["reasons"] = status, reasons
        if dec is None:
            continue
        rec["missing_fundamentals"] = dec.missing_fundamentals
        rec["confidence"] = dec.confidence
        dday = cand.decision_date
        bars = store.bars_after(cand.symbol.upper(), dday)  # strictly after the decision date
        if not bars:
            rec["status"], rec["reasons"] = "skipped", ["no_bars_after_decision"]
            continue
        if (bars[0].date - dday).days > cfg.max_entry_gap_days:
            rec["status"], rec["reasons"] = "skipped", ["stale_market_data"]
            continue
        prior = done.setdefault(cand.group, [])
        open_ = [t for t in prior if t.trade.exit_date >= dday]
        if any(t.trade.symbol == cand.symbol.upper() for t in open_):  # duplicate signal while held
            rec["status"], rec["reasons"] = "skipped", ["duplicate_open_position"]
            continue
        realized = sum(t.trade.pnl for t in prior if t.trade.exit_date < dday)
        equity = initial + realized
        cash = equity - sum(t.entry_notional for t in open_)
        rres = _risk(cand, dec, equity, cash, len(open_), {t.trade.symbol for t in open_})
        if not rres.passed or not rres.computed_qty:
            rec["status"] = "risk_rejected"
            rec["reasons"] = [v.code for v in rres.violations] or ["ZERO_QTY"]
            continue
        sim, outcome = simulate_position(cand.symbol.upper(), dec, rres.computed_qty, cash, bars, cost)
        if sim is None:
            rec["status"], rec["reasons"] = outcome, []
            continue
        t = Trade(cand.symbol.upper(), sim["entry_date"], sim["exit_date"], sim["qty"],
                  round(sim["entry_price"], 4), round(sim["exit_price"], 4), sim["reason"],
                  round(sim["pnl"], 2), round(sim["return_pct"], 4), round(sim["fees"], 4),
                  sim["days_held"], "unknown")
        prior.append(SimTrade(t, cand.group, cand.id, dday, dec.confidence, sim["entry_fee"],
                              sim["entry_notional"], sim["exit_notional"]))
        rec.update(status="filled", entry_date=sim["entry_date"].isoformat(),
                   exit_date=sim["exit_date"].isoformat(), exit_reason=sim["reason"],
                   return_pct=round(sim["return_pct"], 4), qty=sim["qty"])
    return [t for g in sorted(done) for t in done[g]], records


# --------------------------------------------------------------------------- analytics
def calibration(pairs: list[tuple[float, bool]], min_bucket: int = 5) -> dict:
    """Stated confidence vs realized hit rate, bucketed; ECE and Brier over all pairs."""
    buckets = []
    for lo, hi in zip(CALIBRATION_BINS, CALIBRATION_BINS[1:]):
        inb = [(c, h) for c, h in pairs if lo <= c < hi]
        buckets.append({
            "range": f"{lo:.1f}-{min(hi, 1.0):.1f}", "n": len(inb),
            "mean_confidence": round(mean(c for c, _ in inb), 6) if inb else None,
            "hit_rate": round(sum(h for _, h in inb) / len(inb), 6) if inb else None,
            "insufficient": len(inb) < min_bucket})
    n = len(pairs)
    ece = (sum(b["n"] / n * abs(b["mean_confidence"] - b["hit_rate"]) for b in buckets if b["n"])
           if n else None)
    brier = mean((c - h) ** 2 for c, h in pairs) if n else None
    return {"n": n, "buckets": buckets, "ece": None if ece is None else round(ece, 6),
            "brier": None if brier is None else round(brier, 6)}


def equity_curve(store: BarStore, trades: list[SimTrade], dates: list[date], initial: float
                 ) -> tuple[list[tuple[date, float]], list[float]]:
    closes: dict[str, tuple[list[date], list[float]]] = {}
    for st in trades:
        s = st.trade.symbol
        if s not in closes:
            bs = store.bars_after(s, date.min)
            closes[s] = ([b.date for b in bs], [b.close for b in bs])
    curve, exposure = [], []
    for d in dates:
        equity, pos_value = initial, 0.0
        for st in trades:
            t = st.trade
            if d < t.entry_date:
                continue
            if d >= t.exit_date:
                equity += t.pnl
                continue
            ds, cs = closes[t.symbol]
            i = bisect.bisect_right(ds, d) - 1
            px = cs[i] if i >= 0 else t.entry_price
            equity += t.qty * (px - t.entry_price) - st.entry_fee
            pos_value += t.qty * px
        curve.append((d, equity))
        exposure.append(pos_value / equity if equity > 0 else 0.0)
    return curve, exposure


def _group_report(store, sims: list[SimTrade], records: list[dict], dates, cfg: ReplayConfig, bench,
                  baseline: dict | None) -> dict:
    bc = cfg.backtest
    curve, exposure = equity_curve(store, sims, dates, bc.initial_cash)
    trades = [s.trade for s in sims]
    traded = sum(s.entry_notional + s.exit_notional for s in sims)
    m = compute_metrics(curve, trades, bench, traded, exposure, bc.seed, bc.bootstrap_samples)
    pairs = [(s.confidence, s.trade.return_pct > 0) for s in sims if s.confidence is not None]
    counts: dict[str, int] = {}
    for r in records:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    out = {
        "metrics": m, "decisions": dict(sorted(counts.items())),
        "calibration": calibration(pairs, cfg.min_bucket_samples),
        "insufficient_sample": len(sims) < cfg.min_samples, "min_samples": cfg.min_samples,
        "trades": [t.to_dict() for t in trades],
    }
    if baseline is not None:
        out["vs_baseline_return"] = m["net_total_return"] - baseline["metrics"]["net_total_return"]
    out["vs_spy_return"] = m["benchmark_relative_return"]
    return out


def window_report(store: BarStore, sims: list[SimTrade], records: list[dict], dates: list[date],
                  cfg: ReplayConfig, universe: list[str]) -> dict:
    if not dates:
        return {"error": "no trading dates in window"}
    bc = cfg.backtest
    bench = buy_and_hold(store, bc.benchmark, dates, bc)
    try:
        base = run_window(store, universe, dates, bc, include_trades=False) if universe else None
    except ValueError:
        base = None
    lo, hi = dates[0], dates[-1]
    by_group: dict[str, list[SimTrade]] = {}
    rec_group: dict[str, list[dict]] = {}
    for s in sims:
        if lo <= s.decision_date <= hi:
            by_group.setdefault(s.group, []).append(s)
    for r in records:
        d = datetime.fromisoformat(r["decision_ts"]).astimezone(timezone.utc).date()
        if lo <= d <= hi:
            rec_group.setdefault(r["group"], []).append(r)
    groups = {g: _group_report(store, by_group.get(g, []), rec_group[g], dates, cfg, bench, base)
              for g in sorted(rec_group)}
    return {
        "start": lo.isoformat(), "end": hi.isoformat(), "groups": groups,
        "benchmark": {"symbol": bc.benchmark,
                      "total_return": bench[-1][1] / bench[0][1] - 1 if len(bench) > 1 else 0.0},
        "baseline": None if base is None else {"name": "momentum", "metrics": base["metrics"]},
    }


def run_ai_replay(store: BarStore, candidates: list[Candidate], recorded: ai_cache.RecordedOutputs,
                  start: date, end: date, cfg: ReplayConfig | None = None, split: str = "none",
                  universe: list[str] | None = None, train_days: int = 120, test_days: int = 60) -> dict:
    """JSON-serializable report body (no envelope). Deterministic for the same inputs."""
    cfg = cfg or ReplayConfig()
    cands = [c for c in candidates if start <= c.decision_date <= end]
    sims, records = replay_decisions(store, cands, recorded, cfg)
    uni = sorted(universe or {c.symbol.upper() for c in cands})
    dates = [d for d in store.calendar([cfg.backtest.benchmark] + uni) if start <= d <= end]
    windows: dict[str, dict] = {}
    defs: dict[str, dict] = {}
    if split == "none":
        windows["full"] = window_report(store, sims, records, dates, cfg, uni)
    elif split == "holdout":
        wins = chronological_split(dates)
        defs = {k: w.to_dict() for k, w in wins.items()}
        for name in ("development", "validation", "holdout"):  # holdout last, evaluated once
            windows[name] = window_report(store, sims, records, slice_dates(dates, wins[name]), cfg, uni)
    elif split == "walk-forward":
        folds = walk_forward_windows(dates, train_days, test_days)
        if not folds:
            raise ValueError("not enough data for a walk-forward run; reduce train/test days")
        for train, test in folds:
            defs[train.name], defs[test.name] = train.to_dict(), test.to_dict()
            windows[test.name] = window_report(store, sims, records, slice_dates(dates, test), cfg, uni)
    else:
        raise ValueError(f"unknown split '{split}'")
    counts: dict[str, int] = {}
    for r in records:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {
        "split": split, "config": cfg.to_dict(), "universe": uni, "data": store.summary(),
        "window_defs": defs, "windows": windows, "decisions": records,
        "status_counts": dict(sorted(counts.items())),
        "cache": {"entries": len(recorded), "hits": recorded.hits,
                  "misses": sorted(k.label() for k in recorded.misses)},
        "notes": NOTES, "strategy_id_default": STRATEGY_ID,
    }


def summarize(body: dict, start: date, end: date) -> dict:
    c = body["status_counts"]
    return {"start": start.isoformat(), "end": end.isoformat(), "split": body["split"],
            "decisions": sum(c.values()), "simulated": c.get("filled", 0), "no_fill": c.get("no_fill", 0),
            "cache_misses": c.get("cache_miss", 0), "rejected": c.get("rejected", 0),
            "risk_rejected": c.get("risk_rejected", 0), "abstained": c.get("abstained", 0),
            "skipped": c.get("skipped", 0) + c.get("invalid_recorded_output", 0),
            "groups": sorted({r["group"] for r in body["decisions"]})}
