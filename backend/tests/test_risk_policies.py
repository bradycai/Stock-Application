"""Issue #23: adaptive risk policies. Pure-logic tests plus wiring tests with fakes (no network,
no real broker or Claude). Wiring tests that need the database use `pg_db`."""
import random
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.models import AnalysisRun, AuditLog, IntentAction, IntentStatus, TradeIntent
from app.services import (
    automation,
    broker,
    execution,
    market_data,
    monitor,
    risk,
    risk_policies,
    risk_policy_data,
)
from app.services.risk_policies import evaluate_policies

LIQUID = {"last_price": 100.0, "atr_14": 1.0, "avg_volume_20": 5_000_000, "market_open": True}
ACCOUNT = {"equity": 100_000.0}


def _intent(size=5.0, stop=5.0, action="buy", ticker="AAA", limit=100.0):
    return {"ticker": ticker, "action": action, "order_type": "limit", "limit_price": limit,
            "position_size_percent": size, "stop_loss_percent": stop, "take_profit_percent": 10}


def _eval(intent=None, *, positions=(), market=LIQUID, sectors=None, returns=None, **overrides):
    s = Settings(_env_file=None, **overrides)
    sectors = sectors or {}
    return evaluate_policies(intent or _intent(), account=ACCOUNT, positions=list(positions),
                             market=market, sector_of=lambda sym: sectors.get(sym),
                             returns_by_symbol=returns or {}, settings=s)


def _pos(symbol, value):
    return {"symbol": symbol, "market_value": value}


def _codes(res):
    return [v["code"] for v in res.violations]


# --------------------------------------------------------------------------- #
# Each policy                                                                 #
# --------------------------------------------------------------------------- #
def test_typical_intent_passes_untouched():
    res = _eval()
    assert res.violations == [] and res.size_pct == 5.0 and res.notes == []


def test_risk_budget_reduces_size_for_wide_stops():
    res = _eval(_intent(size=10, stop=20))  # 1% budget / 20% stop = 5% of equity
    assert res.violations == [] and res.size_pct == 5.0
    assert any("RISK_BUDGET" in n for n in res.notes)


def test_risk_budget_rejects_when_cut_is_a_sliver():
    res = _eval(_intent(size=10, stop=90))  # 1.11% allowed -> fine; tighten the budget instead
    assert res.violations == [] and res.size_pct == pytest.approx(1.1111, abs=1e-3)
    res = _eval(_intent(size=10, stop=90), risk_per_trade_pct=Decimal("0.1"))  # 0.11% < 0.25% min
    assert _codes(res) == ["RISK_BUDGET"]


def test_high_atr_reduces_size():
    res = _eval(market={**LIQUID, "atr_14": 8.0})  # 8% ATR vs 4% threshold -> half size
    assert res.size_pct == pytest.approx(2.5, abs=1e-3)
    assert res.violations == []


def test_never_above_hard_max_position():
    assert _eval(_intent(size=50, stop=1)).size_pct <= 10.0


def test_gross_exposure_cap_reduces_then_rejects():
    res = _eval(positions=[_pos("X", 47_000)])  # 47% used of 50% -> 3% left
    assert res.violations == [] and res.size_pct == 3.0
    res = _eval(positions=[_pos("X", 50_000)])
    assert _codes(res) == ["EXPOSURE_LIMIT"]


def test_sector_concentration():
    sectors = {"AAA": "Tech", "X": "Tech", "Y": "Energy"}
    res = _eval(positions=[_pos("X", 22_000), _pos("Y", 5_000)], sectors=sectors)  # 3% left in Tech
    assert res.size_pct == 3.0 and res.violations == []
    res = _eval(positions=[_pos("X", 25_000)], sectors=sectors)
    assert _codes(res) == ["SECTOR_CONCENTRATION"]


def test_unknown_sector_is_its_own_bucket():
    # Two unknown-sector holdings do not count against each other or against the candidate.
    res = _eval(positions=[_pos("X", 25_000), _pos("Y", 20_000)])
    assert "SECTOR_CONCENTRATION" not in _codes(res)
    # ...but adding to an existing unknown-sector holding does count against itself.
    res = _eval(_intent(ticker="X"), positions=[_pos("X", 25_000)])
    assert _codes(res) == ["SECTOR_CONCENTRATION"]


def _series(seed, n=40):
    rnd = random.Random(seed)
    return [rnd.uniform(-0.03, 0.03) for _ in range(n)]


def test_correlated_holdings_rejected():
    base = _series(1)
    returns = {"AAA": base, "X": [r * 1.1 for r in base], "Y": [r * 0.9 + 0.0001 for r in base],
               "Z": _series(99)}
    pos = [_pos("X", 5_000), _pos("Y", 5_000), _pos("Z", 5_000)]
    assert _codes(_eval(positions=pos, returns=returns)) == ["CORRELATED_HOLDINGS"]
    # Only one highly correlated holding -> allowed.
    assert _eval(positions=pos[:1] + pos[2:], returns=returns).violations == []
    # Too few observations -> correlation ignored.
    short = {k: v[:5] for k, v in returns.items()}
    assert _eval(positions=pos, returns=short).violations == []


def test_illiquid_rejected_and_adv_cap_reduces():
    assert _codes(_eval(market={**LIQUID, "avg_volume_20": 5_000})) == ["ILLIQUID"]  # $500k ADV
    assert _codes(_eval(market={**LIQUID, "avg_volume_20": None})) == ["ILLIQUID"]  # unknown: fail closed
    # ADV $2M, 1% cap = $20k = 20% of equity -> no cut; ADV $1.2M -> $12k = 12% -> no cut at 5%.
    res = _eval(_intent(size=10, stop=5), market={**LIQUID, "avg_volume_20": 12_000})  # $1.2M ADV
    assert res.violations == [] and res.size_pct == 10.0
    res = _eval(_intent(size=10, stop=5), market={**LIQUID, "avg_volume_20": 12_000},
                max_pct_of_adv=Decimal("0.5"))  # $6k = 6%
    assert res.size_pct == 6.0 and any("ILLIQUID" in n for n in res.notes)


def test_market_closed_is_optional():
    closed = {**LIQUID, "market_open": False}
    assert _eval(market=closed).violations == []  # default off
    assert _codes(_eval(market=closed, block_when_market_closed=True)) == ["MARKET_CLOSED"]
    assert _eval(market=LIQUID, block_when_market_closed=True).violations == []


def test_sells_and_non_buys_pass_through_untouched():
    for action in ("sell", "watch", "avoid"):
        res = _eval(_intent(size=10, stop=50, action=action), market={}, positions=[_pos("X", 90_000)])
        assert res.violations == [] and res.size_pct == 10.0 and res.notes == []


# --------------------------------------------------------------------------- #
# Property: policies never loosen the hard rules                              #
# --------------------------------------------------------------------------- #
def _risk_input(intent, equity, cash, open_positions):
    return risk.RiskInput(intent=intent, equity=Decimal(str(equity)), cash=Decimal(str(cash)),
                          open_positions=open_positions, held_symbols=set(),
                          day_pnl_pct=Decimal("0"), last_price=Decimal(str(intent["limit_price"])),
                          intraday_change_pct=Decimal("0"), is_margin_account=False, is_option=False,
                          trading_halted=False)


def test_property_policies_never_loosen_risk_evaluate():
    rnd = random.Random(23)
    reduced = rejected = 0
    for _ in range(1500):
        equity = rnd.choice([1_000, 25_000, 100_000, 1_000_000])
        intent = _intent(size=rnd.choice([0, 0.1, 1, 5, 9.9, 10, 10.5, 40]),
                         stop=rnd.choice([0, 0.5, 2, 5, 10, 25, 60]),
                         limit=rnd.choice([0.5, 4, 20, 100, 750]),
                         action=rnd.choice(["buy", "buy", "buy", "sell", "watch"]),
                         ticker=rnd.choice(["AAA", "X", "Y"]))
        if rnd.random() < 0.1:
            intent["order_type"] = "market"
        cash = equity * rnd.choice([0.02, 0.5, 1])
        ri = _risk_input(intent, equity, cash, rnd.randint(0, 4))
        baseline = risk.evaluate(ri)
        before = dict(intent)
        market = {"last_price": intent["limit_price"], "atr_14": rnd.choice([None, 0.2, 2, 20]) and
                  rnd.uniform(0.001, 0.2) * intent["limit_price"],
                  "avg_volume_20": rnd.choice([None, 1_000, 1e5, 1e7]),
                  "market_open": rnd.choice([True, False, None])}
        held = rnd.sample(["X", "Y", "Z"], rnd.randint(0, 3))
        positions = [_pos(s, rnd.uniform(0, equity * 0.3)) for s in held]
        returns = {s: _series(rnd.randint(0, 3)) for s in ("AAA", "X", "Y", "Z")}
        intent_copy = dict(intent)
        ri2 = _risk_input(intent_copy, equity, cash, ri.open_positions)
        result, _notes = risk_policies.apply(
            intent_copy, ri2, baseline, positions=positions, market=market,
            sector_of=lambda s: rnd.choice(["Tech", None]), returns_by_symbol=returns)
        if not baseline.passed:
            assert result is baseline and not result.passed  # nothing a policy does can approve it
            assert intent_copy == before
            continue
        if intent["action"] != "buy":
            assert result is baseline and intent_copy == before  # sells pass through untouched
            continue
        if result.passed:
            assert result.computed_qty is not None and baseline.computed_qty is not None
            assert result.computed_qty <= baseline.computed_qty
            assert result.computed_notional <= baseline.computed_notional
            assert intent_copy["position_size_percent"] <= intent["position_size_percent"]
            assert intent_copy["position_size_percent"] <= float(risk.settings.max_position_pct)
            assert risk.evaluate(ri2).passed  # the reduced intent passes the hard rules on its own
            reduced += result.computed_qty < baseline.computed_qty
        else:
            rejected += 1
    assert reduced > 20 and rejected > 20  # the generator exercised both outcomes


# --------------------------------------------------------------------------- #
# Wiring: monitor                                                             #
# --------------------------------------------------------------------------- #
def _persist(positions=(), mdata=None, size=10, stop=20):
    from app.schemas import MonitorDecision
    from app.services.triggers import Trigger

    decision = MonitorDecision(ticker="AAPL", action="buy", confidence=0.9, reason="breakout",
                               size_percent=size, stop_loss_percent=stop, take_profit_percent=30,
                               limit_price=100.0)
    db = SimpleNamespace(add=lambda _: None, flush=lambda: None)
    account = {"equity": 100_000, "cash": 100_000, "day_pnl_pct": 0.0, "open_positions": len(positions)}
    md = mdata if mdata is not None else {
        "last_price": 100.0, "intraday_change_pct": 0.0,
        "quality": {"stale": False, "incomplete": False, "missing": []},
        "indicators": {"atr_14": 1.0, "avg_volume_20": 5_000_000}}
    return monitor._persist_buy_intent(
        db, SimpleNamespace(id=1), "AAPL", decision, {"price": 100.0}, md, account,
        {p["symbol"] for p in positions}, False,
        fired=[Trigger("VOLUME_SPIKE", "relative volume 3.0x")], data_age_sec=1, positions=list(positions))


def test_monitor_buy_is_reduced_by_risk_budget_and_qty_recomputed():
    row = _persist()
    assert row.status == IntentStatus.pending_approval, row.risk_violations
    assert row.position_size_percent == 5.0 and row.computed_qty == 50  # 5% of 100k at $100


def test_monitor_buy_rejected_by_policy_carries_codes():
    row = _persist(positions=[_pos("MSFT", 60_000)])
    assert row.status == IntentStatus.rejected_by_risk and row.risk_passed is False
    assert "EXPOSURE_LIMIT" in {v["code"] for v in row.risk_violations}
    assert row.computed_qty is None


def test_monitor_buy_without_volume_data_is_rejected_illiquid():
    row = _persist(mdata={"last_price": 100.0, "intraday_change_pct": 0.0,
                         "quality": {"stale": False, "incomplete": False, "missing": []}})
    assert row.status == IntentStatus.rejected_by_risk
    assert "ILLIQUID" in {v["code"] for v in row.risk_violations}


# --------------------------------------------------------------------------- #
# Wiring: execution (database)                                                #
# --------------------------------------------------------------------------- #
@pytest.fixture()
def exec_env(monkeypatch):
    gate = automation.evaluate_gate(mode="manual", kill_switch=False, capability_ok=True,
                                    capability_reason="ok")
    monkeypatch.setattr(automation, "order_gate", lambda db: gate)
    env = SimpleNamespace(positions=[], submitted=[], market=dict(LIQUID))
    now = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(broker, "get_account", lambda: {
        "equity": 100_000.0, "cash": 100_000.0, "buying_power": 100_000.0, "day_pnl_pct": 0.0,
        "is_paper": True, "is_margin": False})
    monkeypatch.setattr(broker, "get_positions", lambda: env.positions)
    monkeypatch.setattr(broker, "get_quote_and_intraday",
                        lambda s: {"last_price": 100.0, "intraday_change_pct": 0.0})
    monkeypatch.setattr(market_data, "fetch_snapshots",
                        lambda syms: {syms[0]: {"last_price": 100.0, "quote_ts": now, "last_trade_ts": now}})
    monkeypatch.setattr(market_data, "market_clock", lambda *a, **k: {"is_open": True})
    monkeypatch.setattr(risk_policy_data, "market_from_bars", lambda db, sym, price: env.market)

    def submit(intent, qty, client_order_id):
        env.submitted.append((qty, client_order_id))
        return {"broker_order_id": "o1", "client_order_id": client_order_id, "symbol": intent["ticker"],
                "side": "buy", "qty": qty, "order_type": "limit", "limit_price": intent["limit_price"],
                "stop_loss_price": 80.0, "take_profit_price": None, "status": "accepted", "raw": {}}
    monkeypatch.setattr(broker, "submit_bracket_order", submit)
    return env


def _db_buy(db, size=10, stop=20):
    run = AnalysisRun(model="m", input_context={}, raw_response={})
    db.add(run)
    db.flush()
    row = TradeIntent(run_id=run.id, ticker="AAPL", action=IntentAction.buy,
                      status=IntentStatus.pending_approval, created_at=datetime.now(timezone.utc),
                      limit_price=100.0, order_type="limit", position_size_percent=size,
                      stop_loss_percent=stop, take_profit_percent=30, confidence=0.8)
    db.add(row)
    db.flush()
    return row


def _audits(db, event):
    from sqlalchemy import select
    return db.scalars(select(AuditLog).where(AuditLog.event_type == event)).all()


def test_execution_submits_reduced_qty_and_audits(pg_db, exec_env):
    intent = _db_buy(pg_db)  # 10% size with a 20% stop -> 5%
    execution.execute_intent(pg_db, intent, "user:operator")
    assert exec_env.submitted[0][0] == 50 and intent.computed_qty == 50
    assert intent.position_size_percent == 5.0 and intent.status == IntentStatus.submitted
    assert _audits(pg_db, "risk_policy_adjusted")[0].payload["to"] == 5.0


def test_execution_rejects_on_fresh_exposure_and_never_submits(pg_db, exec_env):
    exec_env.positions = [{"symbol": "MSFT", "qty": 1, "market_value": 60_000.0}]
    intent = _db_buy(pg_db)
    with pytest.raises(execution.ExecutionError) as e:
        execution.execute_intent(pg_db, intent, "user:operator")
    assert e.value.status_code == 422
    assert "EXPOSURE_LIMIT" in {v["code"] for v in e.value.violations}
    assert exec_env.submitted == [] and intent.status == IntentStatus.rejected_by_risk
    assert _audits(pg_db, "risk_rejection")


def test_execution_fails_closed_when_policies_error(pg_db, exec_env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("policy data exploded")
    monkeypatch.setattr(risk_policies, "apply", boom)
    intent = _db_buy(pg_db)
    with pytest.raises(execution.ExecutionError) as e:
        execution.execute_intent(pg_db, intent, "user:operator")
    assert "RISK_POLICY_ERROR" in str(e.value.detail) and exec_env.submitted == []
    assert intent.status == IntentStatus.pending_approval
    assert _audits(pg_db, "entry_blocked")[-1].payload["code"] == "RISK_POLICY_ERROR"


# --------------------------------------------------------------------------- #
# Wiring: analysis (database)                                                 #
# --------------------------------------------------------------------------- #
def test_analysis_applies_reduced_size_and_policy_rejections(pg_db, monkeypatch):
    from app.routers import analysis
    from app.schemas import RunAnalysisRequest, TradeIntent as ModelIntent
    from app.services import batch, data_aggregator, prescreener

    def model_intent(ticker):
        return ModelIntent(ticker=ticker, action="buy", order_type="limit", limit_price=100.0,
                           position_size_percent=10, stop_loss_percent=20, take_profit_percent=30,
                           confidence=0.8, reason="r", risks="r", evidence=["rsi_14=55"])

    def entry(volume):
        return {"last_price": 100.0, "intraday_change_pct": 0.0, "sector": "Tech", "news": [],
                "quality": {"stale": False, "incomplete": False, "missing": []},
                "indicators": {"data_sufficient": True, "atr_14": 1.0, "avg_volume_20": volume}}

    watch = {"GOOD": entry(5_000_000), "THIN": entry(1_000)}
    account = {"equity": 100_000.0, "cash": 100_000.0, "buying_power": 100_000.0, "day_pnl": 0.0,
               "day_pnl_pct": 0.0, "is_paper": True, "raw": {}}
    context = {"portfolio": {"open_positions": 0}, "positions": [], "market": {"open": True},
               "watchlist_data": watch}
    monkeypatch.setattr(data_aggregator, "build_context", lambda syms, **kw: (context, account))
    monkeypatch.setattr(prescreener, "rank_candidates",
                        lambda wd, n: [SimpleNamespace(symbol=s) for s in wd])
    stats = {"batches": 1, "failed_batches": 0}
    monkeypatch.setattr(batch, "run_batched", lambda *a, **k: (
        [model_intent("GOOD"), model_intent("THIN")], stats))
    gate = automation.evaluate_gate(mode="manual", kill_switch=False, capability_ok=True,
                                    capability_reason="ok")
    monkeypatch.setattr(automation, "order_gate", lambda db: gate)
    monkeypatch.setattr(analysis.risk_state, "is_halted", lambda db, acct: (False, None))

    out = analysis._run(pg_db, RunAnalysisRequest(), ["GOOD", "THIN"], "analysis-test")
    by_ticker = {i.ticker: i for i in out.intents}
    good, thin = by_ticker["GOOD"], by_ticker["THIN"]
    assert good.status == "pending_approval" and good.position_size_percent == 5.0
    assert good.computed_qty == 50
    assert thin.status == "rejected_by_risk"
    assert "ILLIQUID" in {v.code for v in thin.risk_violations}


def test_settings_defaults_are_conservative():
    s = Settings(_env_file=None)
    assert s.block_when_market_closed is False
    assert s.risk_per_trade_pct == Decimal("1.0") and s.max_gross_exposure_pct == Decimal("50")
