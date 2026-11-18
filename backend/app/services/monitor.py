"""Position + market monitor.

Two layers run on one timer:

1. Hard safety backstop (always, whenever auto-trading OR the continuous monitor is on):
   daily-loss halt + hard stop-loss / take-profit auto-exit from the stored bracket
   levels. This is the dependable floor and does not involve Claude.

2. Continuous AI monitor (when CONTINUOUS_MONITOR_ENABLED): every cycle it builds a cheap
   snapshot of each monitored ticker (positions + open orders + watchlist), runs
   rule-based triggers, and ONLY when a trigger fires asks Claude for a decision
   (hold/buy/sell/trim/add/tighten_stop/raise_take_profit). Buys go through the risk
   engine + the existing execute path; sells/trims auto-execute in paper when confidence
   clears AUTO_SELL_CONFIDENCE. Hard stop/take always remain as backup.

Every broker call here goes through `_may_order(gate, ...)`: an order is placed only when
the automation gate allows it (env capability + mode "auto" + kill switch off). In
observe mode the would-be order is recorded instead; in manual mode it is only logged.

Pure, unit-tested pieces: `evaluate_exit`, `daily_loss_breached`, `detect_triggers`
(in triggers.py), `plan_monitor_action`, and `_may_order`.
"""
import asyncio
import logging
import math
from datetime import datetime, timezone

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.deps import audit
from app.logging_config import log_event, run_id_var, safe_error
from app.models import (
    AnalysisRun,
    IntentAction,
    IntentStatus,
    Order,
    PositionManagement,
    TradeIntent,
    Watchlist,
)
from app.services import (
    analyst,
    automation,
    broker,
    data_aggregator,
    intent_validation,
    order_sync,
    position_manager,
    risk,
    risk_state,
    triggers,
)
from app.services.execution import ExecutionError, execute_intent
from app.services.orchestrator import build_risk_input
from app.services.recommendations import clamp_position_size

logger = logging.getLogger("trading.monitor")

_task: asyncio.Task | None = None

# In-memory per-symbol last snapshot (for change-based triggers) and run status.
_LAST_STATE: dict[str, dict] = {}
MONITOR_STATUS: dict = {
    "last_check_at": None, "monitored": 0, "dropped": 0, "watchlist_total": 0,
    "triggers": 0, "claude_calls": 0, "auto_sells": 0, "partials": 0, "buys": 0,
    "ignored": 0, "errors": 0,
}

DEFAULT_STOP_PCT = 5.0
DEFAULT_TAKE_PCT = 10.0


# --------------------------------------------------------------------------- #
# Pure decision logic                                                         #
# --------------------------------------------------------------------------- #
def evaluate_exit(current_price, stop_price, take_price):
    """Return 'stop_loss', 'take_profit', or None."""
    if current_price is None or current_price <= 0:
        return None
    if stop_price is not None and current_price <= stop_price:
        return "stop_loss"
    if take_price is not None and current_price >= take_price:
        return "take_profit"
    return None


def daily_loss_breached(day_pnl_pct: float, limit_pct: float) -> bool:
    return day_pnl_pct <= -abs(limit_pct)


def plan_monitor_action(action, confidence_pct, auto_sell_conf, auto_buy_conf,
                        has_position, auto_buy_allowed):
    """Map a Claude monitor decision to a concrete plan. Pure → unit-tested.

    Returns (plan, reason) where plan ∈ {hold, auto_sell, auto_trim, auto_buy,
    manual_review, tighten_stop, raise_take_profit, ignored_low_confidence,
    ignored_no_position, ignored}.
    """
    if action == "hold":
        return "hold", "Claude recommended hold"

    if action in ("sell", "trim"):
        if not has_position:
            return "ignored_no_position", f"no position to {action}"
        if confidence_pct >= auto_sell_conf:
            return ("auto_sell" if action == "sell" else "auto_trim",
                    f"confidence {confidence_pct:.0f}% >= {auto_sell_conf:.0f}%")
        return "ignored_low_confidence", f"{action} confidence {confidence_pct:.0f}% < {auto_sell_conf:.0f}%"

    if action in ("buy", "add"):
        if not auto_buy_allowed:
            return "manual_review", "auto-buy gate is off — left for manual approval"
        if confidence_pct >= auto_buy_conf:
            return "auto_buy", f"confidence {confidence_pct:.0f}% >= {auto_buy_conf:.0f}%"
        return "ignored_low_confidence", f"buy confidence {confidence_pct:.0f}% < {auto_buy_conf:.0f}%"

    if action == "tighten_stop":
        return "tighten_stop", "raise protective stop"
    if action == "raise_take_profit":
        return "raise_take_profit", "lift take-profit"
    return "ignored", f"unhandled action '{action}'"


# --------------------------------------------------------------------------- #
# Capability gate                                                             #
# --------------------------------------------------------------------------- #
def continuous_capability(s=settings) -> tuple[bool, str]:
    """Continuous monitor is paper-only unless live is explicitly confirmed."""
    if not s.continuous_monitor_enabled:
        return False, "CONTINUOUS_MONITOR_ENABLED is false"
    if s.is_live and not s.live_auto_confirmed:
        return False, "live monitor requires AUTO_LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_THE_RISK"
    return True, "ok"


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
def _latest_protective_order(db, symbol: str) -> Order | None:
    return db.scalars(
        select(Order).where(Order.symbol == symbol).order_by(Order.submitted_at.desc())
    ).first()


def _snapshot(symbol, mdata, position, order) -> dict:
    ind = (mdata or {}).get("indicators") or {}
    return {
        "price": (mdata or {}).get("last_price") or ind.get("last_close"),
        "pnl_pct": position["unrealized_plpc"] if position else None,
        "rel_volume": ind.get("relative_volume"),
        "rsi": ind.get("rsi_14"),
        "macd_hist": (ind.get("macd") or {}).get("histogram"),
        "news_count": len((mdata or {}).get("news") or []),
        "stop_price": float(order.stop_loss_price) if order and order.stop_loss_price else None,
        "take_price": float(order.take_profit_price) if order and order.take_profit_price else None,
    }


def _news_items(mdata, limit: int = 3) -> list[dict]:
    """The newest news items for the model: citable id, headline, event type, is_new."""
    return [{k: n.get(k) for k in ("id", "headline", "event_type", "is_new", "published_at")}
            for n in ((mdata or {}).get("news") or [])[:limit]]


def _compact_payload(symbol, snap, mdata, position, fired):
    ind = (mdata or {}).get("indicators") or {}
    return {
        "ticker": symbol,
        "price": snap["price"],
        "position": (
            {"qty": position["qty"], "avg_entry": position["avg_entry_price"],
             "pnl_pct": round(position["unrealized_plpc"], 2)} if position else None
        ),
        "indicators": {
            "rsi_14": ind.get("rsi_14"), "macd_hist": (ind.get("macd") or {}).get("histogram"),
            "relative_volume": ind.get("relative_volume"),
            "sma_20": ind.get("sma_20"), "sma_50": ind.get("sma_50"), "sma_200": ind.get("sma_200"),
            "return_5d_pct": ind.get("return_5d_pct"), "return_20d_pct": ind.get("return_20d_pct"),
        },
        "stop_price": snap["stop_price"], "take_price": snap["take_price"],
        "triggers": [t.as_dict() for t in fired],
        "news": _news_items(mdata),
        "quality": (mdata or {}).get("quality"),
        "max_position_pct": float(settings.max_position_pct),
    }


def _get_monitor_run(db) -> AnalysisRun:
    run = AnalysisRun(
        model=settings.position_model or settings.strategist_model, prompt_tokens=0, output_tokens=0,
        input_context={"source": "continuous_monitor"}, raw_response={},
    )
    db.add(run)
    db.flush()
    return run


# Observe-mode records are rate-limited per (symbol, action) so a condition that persists
# across cycles (e.g. a trailing stop that stays hit) doesn't flood the audit log.
OBSERVE_COOLDOWN_SEC = 1800
_LAST_OBSERVED: dict[tuple[str, str], datetime] = {}


def _may_order(db, action: str, symbol: str | None = None, **payload) -> bool:
    """The monitor's single checkpoint before any broker call. True = place the order.

    The gate is re-read from the database on every call, so a kill switch or mode change
    committed mid-cycle takes effect before the next order. Otherwise nothing is sent:
    observe mode records an `observe_would_execute` audit event (rate-limited), and manual
    mode / kill switch / missing capability is only logged.
    """
    gate = automation.order_gate(db)
    if gate.allowed:
        return True
    fields = {"action": action, "symbol": symbol, "reason": gate.reason, **payload}
    if gate.observe:
        key, now = (symbol or "*", action), datetime.now(timezone.utc)
        last = _LAST_OBSERVED.get(key)
        if last is None or (now - last).total_seconds() >= OBSERVE_COOLDOWN_SEC:
            _LAST_OBSERVED[key] = now
            audit(db, "observe_would_execute", "monitor", payload=fields)
        log_event(logger, "monitor.observe_would_execute", **fields)
    else:
        log_event(logger, "monitor.order_blocked", **fields)
    return False


# --------------------------------------------------------------------------- #
# Orchestration                                                               #
# --------------------------------------------------------------------------- #
def check_once(db) -> dict:
    MONITOR_STATUS["last_check_at"] = datetime.now(timezone.utc).isoformat()

    gate = automation.order_gate(db)
    cont_ok, _cr = continuous_capability()
    if not (gate.allowed or gate.observe or cont_ok):
        return {"skipped": True, "reason": gate.reason}

    try:
        account = broker.get_account()
        positions = broker.get_positions()
    except Exception as e:  # noqa: BLE001
        return {"error": f"broker: {e}"}

    now = datetime.now(timezone.utc)

    # 1. Hard safety backstop (runs whenever either subsystem is on).
    if daily_loss_breached(account["day_pnl_pct"], float(settings.daily_loss_limit_pct)):
        risk_state.halt(db, f"Daily loss limit hit ({account['day_pnl_pct']:.2f}%)", account)
        # Liquidate first (only if the gate allows it), THEN drop automation to manual —
        # the reverse order would make the gate block the liquidation itself.
        closed = []
        if _may_order(db, "daily_loss_liquidation", symbols=[p["symbol"] for p in positions],
                      day_pnl_pct=account["day_pnl_pct"]):
            for p in positions:
                try:
                    broker.close_position(p["symbol"])
                    closed.append(p["symbol"])
                except Exception:  # noqa: BLE001
                    pass
        if automation.get_state(db).mode == "auto":
            automation.set_mode(db, "manual", "monitor")
        audit(db, "daily_loss_halt", "monitor",
              payload={"day_pnl_pct": account["day_pnl_pct"], "closed": closed})
        for sym in closed:
            audit(db, "auto_sell_executed", "monitor", payload={"symbol": sym, "reason": "daily_loss"})
        db.commit()
        return {"halted": True, "closed": closed}

    for p in positions:
        order = _latest_protective_order(db, p["symbol"])
        if order is None:
            continue
        stop = float(order.stop_loss_price) if order.stop_loss_price is not None else None
        take = float(order.take_profit_price) if order.take_profit_price is not None else None
        decision = evaluate_exit(p["current_price"], stop, take)
        if decision and order.monitoring_status != "exited":
            if not _may_order(db, decision, p["symbol"], price=p["current_price"],
                              stop=stop, take=take):
                order.last_checked_at = now
                continue
            try:
                broker.close_position(p["symbol"])
            except Exception as e:  # noqa: BLE001
                audit(db, "order_error", "monitor", order_id=order.id,
                      payload={"error": str(e), "symbol": p["symbol"]})
                continue
            order.exit_reason = decision
            order.monitoring_status = "exited"
            order.last_checked_at = now
            if order.intent:
                order.intent.status = IntentStatus.filled
            event = "stop_loss_triggered" if decision == "stop_loss" else "take_profit_triggered"
            audit(db, event, "monitor", order_id=order.id,
                  payload={"symbol": p["symbol"], "price": p["current_price"], "stop": stop, "take": take})
            audit(db, "auto_sell_executed", "monitor", order_id=order.id,
                  payload={"symbol": p["symbol"], "reason": decision})
            db.commit()  # the order was sent: persist its record before anything else can fail
        else:
            order.last_checked_at = now
            order.monitoring_status = order.monitoring_status or "active"

    # 2. Continuous AI monitor (only when enabled).
    result = {"checked": len(positions)}
    if cont_ok:
        result["continuous"] = _run_continuous(db, account, positions, gate)

    db.commit()
    return result


def _run_continuous(db, account, positions, gate: automation.OrderGate) -> dict:
    pos_by_symbol = {p["symbol"]: p for p in positions}
    # Deterministic watchlist order (by insertion id) so the cap is predictable.
    watch = list(
        db.scalars(
            select(Watchlist.ticker).where(Watchlist.active.is_(True)).order_by(Watchlist.id)
        ).all()
    )
    try:
        open_syms = broker.get_open_order_symbols()
    except Exception:  # noqa: BLE001
        open_syms = []

    # Positions + open orders are ALWAYS monitored (prepended); watchlist fills the rest.
    combined = list(dict.fromkeys(list(pos_by_symbol) + open_syms + watch))
    cap = settings.monitor_max_symbols
    if cap and cap > 0:
        monitored = combined[:cap]
        dropped = combined[cap:]
        if dropped:
            logger.warning(
                "MONITOR cap reached: monitoring %d/%d symbols (MONITOR_MAX_SYMBOLS=%d). "
                "%d watchlist symbol(s) NOT monitored this cycle: %s. "
                "Set MONITOR_MAX_SYMBOLS=0 to monitor all.",
                len(monitored), len(combined), cap, len(dropped), dropped,
            )
    else:
        monitored = combined  # 0 / negative => no cap, monitor everything
        dropped = []
    counts = {"monitored": len(monitored), "dropped": len(dropped), "watchlist_total": len(watch),
              "triggers": 0, "claude_calls": 0, "auto_sells": 0, "partials": 0, "buys": 0,
              "ignored": 0, "errors": 0}
    if not monitored:
        _update_status(counts)
        return counts

    data_as_of = datetime.now(timezone.utc)
    try:
        context, _acct = data_aggregator.build_context(monitored, db=db, run_id=run_id_var.get())
        db.commit()  # persist news/bars/quotes now, so a per-symbol rollback can't orphan news ids
    except Exception as e:  # noqa: BLE001
        counts["errors"] += 1
        audit(db, "monitor_error", "monitor", payload={"error": f"data: {e}"})
        _update_status(counts)
        return counts

    market = context["watchlist_data"]
    held = set(pos_by_symbol)
    halted, _ = risk_state.is_halted(db, account)
    auto_sell_conf = float(settings.auto_sell_confidence)
    auto_buy_conf = float(settings.auto_buy_confidence)

    run_holder: dict = {"run": None}  # the monitor's AnalysisRun, created on first buy intent

    def handle(symbol: str) -> None:
        mdata = market.get(symbol, {})
        order = _latest_protective_order(db, symbol)
        position = pos_by_symbol.get(symbol)

        # HELD positions -> AI exit manager (trailing stop, partials, thesis re-eval).
        if position:
            _manage_position(db, symbol, position, mdata, counts)
            return

        # WATCHLIST (not held) -> trigger -> buy/watch flow (unchanged).
        snap = _snapshot(symbol, mdata, position, order)
        fired = triggers.detect_triggers(_LAST_STATE.get(symbol), snap)
        _LAST_STATE[symbol] = snap
        if not fired:
            return

        counts["triggers"] += len(fired)
        audit(db, "monitor_trigger", "monitor",
              payload={"symbol": symbol, "triggers": [t.as_dict() for t in fired]})

        model_input = _compact_payload(symbol, snap, mdata, position, fired)
        decision, meta, ok = analyst.monitor_decision(model_input)
        counts["claude_calls"] += 1
        if not ok or decision is None:
            counts["errors"] += 1
            audit(db, "monitor_error", "monitor",
                  payload={"symbol": symbol, "error": meta.get("error")})
            return

        conf_pct = decision.confidence * 100
        audit(db, "monitor_decision", "monitor",
              payload={"symbol": symbol, "action": decision.action,
                       "confidence": round(conf_pct, 1), "reason": decision.reason,
                       "prompt_version": meta.get("prompt_version"), "model": meta.get("served_model"),
                       "input": model_input})

        plan, why = plan_monitor_action(
            decision.action, conf_pct, auto_sell_conf, auto_buy_conf,
            has_position=symbol in pos_by_symbol, auto_buy_allowed=gate.allowed or gate.observe,
        )

        if plan in ("auto_sell", "auto_trim"):
            qty = None
            if plan == "auto_trim" and position:
                qty = max(1, int(math.floor(position["qty"] / 2)))
            if not _may_order(db, plan, symbol, qty=qty, confidence=round(conf_pct, 1)):
                counts["ignored"] += 1
                return
            try:
                broker.close_position(symbol, qty=qty)
            except Exception as e:  # noqa: BLE001
                counts["errors"] += 1
                audit(db, "monitor_error", "monitor", payload={"symbol": symbol, "error": str(e)})
                return
            if order and plan == "auto_sell":
                order.exit_reason = "claude_sell"
                order.monitoring_status = "exited"
                if order.intent:
                    order.intent.status = IntentStatus.filled
            counts["auto_sells"] += 1
            audit(db, "monitor_auto_sell", "monitor",
                  payload={"symbol": symbol, "kind": plan, "qty": qty,
                           "confidence": round(conf_pct, 1), "reason": decision.reason})
            audit(db, "auto_sell_executed", "monitor",
                  payload={"symbol": symbol, "reason": "claude_" + decision.action})

        elif plan in ("auto_buy", "manual_review"):
            run_holder["run"] = run_holder["run"] or _get_monitor_run(db)
            data_age_sec = (datetime.now(timezone.utc) - data_as_of).total_seconds()
            row = _persist_buy_intent(db, run_holder["run"], symbol, decision, snap, mdata,
                                      account, held, halted, fired=fired, data_age_sec=data_age_sec)
            if row.status == IntentStatus.rejected_by_risk:
                counts["ignored"] += 1
                audit(db, "monitor_ignored", "monitor",
                      payload={"symbol": symbol, "reason": "validation/risk rejected",
                               "action": decision.action,
                               "violations": row.risk_violations})
            elif plan == "auto_buy" and not _may_order(db, "auto_buy", symbol, intent_id=row.id,
                                                       confidence=round(conf_pct, 1)):
                counts["ignored"] += 1  # left pending for manual approval
            elif plan == "auto_buy":
                try:
                    execute_intent(db, row, actor="monitor", auto=True)
                    counts["buys"] += 1
                except ExecutionError as e:
                    counts["ignored"] += 1
                    audit(db, "monitor_ignored", "monitor",
                          payload={"symbol": symbol, "reason": str(e.detail), "action": decision.action})
            else:  # manual_review — leave as pending_approval
                counts["ignored"] += 1
                audit(db, "monitor_ignored", "monitor",
                      payload={"symbol": symbol, "reason": why, "action": decision.action})

        elif plan == "tighten_stop":
            _adjust_protection(db, order, snap, decision, counts, kind="tighten_stop")

        elif plan == "raise_take_profit":
            _adjust_protection(db, order, snap, decision, counts, kind="raise_take_profit")

        else:  # hold / ignored_*
            counts["ignored"] += 1
            audit(db, "monitor_ignored", "monitor",
                  payload={"symbol": symbol, "reason": why, "action": decision.action,
                           "confidence": round(conf_pct, 1)})


    # Each symbol is its own unit of work: commit after it, so an order sent for one symbol
    # (and the state/audit that records it) can never be rolled back by a later failure.
    for symbol in monitored:
        try:
            handle(symbol)
            db.commit()
        except Exception as e:  # noqa: BLE001
            db.rollback()
            run_holder["run"] = None
            counts["errors"] += 1
            log_event(logger, "monitor.symbol_failed", logging.ERROR, symbol=symbol,
                      error=safe_error(e))
            audit(db, "monitor_error", "monitor",
                  payload={"symbol": symbol, "error": safe_error(e)})
            db.commit()
    _update_status(counts)
    return counts


def _seed_position(db, symbol: str, position: dict) -> PositionManagement:
    """Get (or create) the AI-management record for a held symbol."""
    pm = db.scalars(
        select(PositionManagement).where(
            PositionManagement.symbol == symbol, PositionManagement.is_open.is_(True)
        )
    ).first()
    if pm:
        return pm
    entry = float(position["avg_entry_price"])
    cur = float(position.get("current_price") or entry)
    qty = int(position["qty"])
    intent = db.scalars(
        select(TradeIntent)
        .where(TradeIntent.ticker == symbol, TradeIntent.action == IntentAction.buy)
        .order_by(TradeIntent.created_at.desc())
    ).first()
    stop_pct = (
        float(intent.stop_loss_percent)
        if intent and intent.stop_loss_percent
        else float(settings.trailing_stop_percent)
    )
    initial_stop = round(entry * (1 - stop_pct / 100), 2)
    pm = PositionManagement(
        symbol=symbol, entry_price=entry, original_qty=qty, highest_price=max(entry, cur),
        initial_stop_price=initial_stop, trailing_stop_price=initial_stop,
        broker_stop_price=initial_stop,
        original_thesis=(intent.reason if intent else None),
        current_thesis=(intent.reason if intent else None),
        partial_levels_taken=[], intent_id=(intent.id if intent else None),
    )
    db.add(pm)
    db.flush()
    audit(db, "position_managed_opened", "monitor",
          payload={"symbol": symbol, "entry": entry, "qty": qty, "initial_stop": initial_stop})
    return pm


def _managed_sell_all(db, pm: PositionManagement, symbol: str, reason: str, counts: dict,
                      price: float) -> bool:
    """Close the whole position. Returns True only if the order was sent."""
    if not _may_order(db, reason, symbol, price=price, full=True):
        counts["ignored"] += 1
        return False
    try:
        broker.cancel_open_orders(symbol)
        broker.close_position(symbol)
    except Exception as e:  # noqa: BLE001
        counts["errors"] += 1
        audit(db, "monitor_error", "monitor", payload={"symbol": symbol, "error": str(e)})
        return False
    pm.is_open = False
    pm.closed_at = datetime.now(timezone.utc)
    pm.last_decision = reason
    counts["auto_sells"] += 1
    audit(db, "monitor_auto_sell", "monitor",
          payload={"symbol": symbol, "kind": reason, "full": True, "price": price})
    audit(db, "auto_sell_executed", "monitor", payload={"symbol": symbol, "reason": reason})
    return True


def _managed_sell_partial(db, pm: PositionManagement, symbol: str, position: dict, qty: int,
                          reason: str, counts: dict, price: float) -> bool:
    """Sell part of the position and re-arm a protective stop. True only if sent."""
    if not _may_order(db, reason, symbol, qty=qty, price=price, partial=True):
        counts["ignored"] += 1
        return False
    try:
        broker.cancel_open_orders(symbol)
        broker.close_position(symbol, qty=qty)
    except Exception as e:  # noqa: BLE001
        counts["errors"] += 1
        audit(db, "monitor_error", "monitor", payload={"symbol": symbol, "error": str(e)})
        return False
    counts["partials"] += 1
    audit(db, "monitor_auto_sell", "monitor",
          payload={"symbol": symbol, "kind": reason, "qty": qty, "partial": True, "price": price})
    audit(db, "auto_sell_executed", "monitor", payload={"symbol": symbol, "reason": reason})
    # Re-arm a broker-side protective stop on the remaining shares (best-effort).
    remaining = max(0, int(position["qty"]) - qty)
    if remaining > 0 and pm.trailing_stop_price:
        try:
            broker.place_stop_order(symbol, remaining, float(pm.trailing_stop_price))
            pm.broker_stop_price = float(pm.trailing_stop_price)
        except Exception:  # noqa: BLE001
            pass
    return True


def _position_payload(symbol, pm, position, mdata, trigs) -> dict:
    ind = (mdata or {}).get("indicators") or {}
    price = float(position.get("current_price") or 0)
    return {
        "ticker": symbol,
        "current_price": price,
        "entry_price": float(pm.entry_price),
        "highest_price": float(pm.highest_price),
        "gain_pct": round(position_manager.gain_pct(float(pm.entry_price), price), 2),
        "pnl_pct": round(position.get("unrealized_plpc") or 0, 2),
        "trailing_stop": float(pm.trailing_stop_price) if pm.trailing_stop_price else None,
        "partials_taken": list(pm.partial_levels_taken or []),
        "original_thesis": pm.original_thesis,
        "current_thesis": pm.current_thesis,
        "indicators": {
            "rsi_14": ind.get("rsi_14"), "macd_hist": (ind.get("macd") or {}).get("histogram"),
            "atr_14": ind.get("atr_14"), "relative_volume": ind.get("relative_volume"),
            "sma_20": ind.get("sma_20"), "sma_50": ind.get("sma_50"), "sma_200": ind.get("sma_200"),
            "return_5d_pct": ind.get("return_5d_pct"), "return_20d_pct": ind.get("return_20d_pct"),
        },
        "news": _news_items(mdata),
        "quality": (mdata or {}).get("quality"),
        "triggers": trigs,
    }


def _manage_position(db, symbol: str, position: dict, mdata: dict, counts: dict) -> None:
    """AI exit management for ONE held position: update trailing stop, take partial
    profits, enforce the trailing/hard stop, and re-evaluate the thesis with Claude on
    triggers. Sell rules: trailing stop / Claude SELL (>= AI_EXIT_CONFIDENCE) sell all;
    partial levels and Claude TRIM sell part; Claude HOLD does nothing."""
    price = float(position.get("current_price") or mdata.get("last_price") or 0)
    if price <= 0:
        return
    pm = _seed_position(db, symbol, position)
    now = datetime.now(timezone.utc)
    ind = (mdata or {}).get("indicators") or {}
    atr = ind.get("atr_14")

    # Advance the trailing stop (only ever up).
    prev_high = float(pm.highest_price)
    new_high = price > prev_high
    if new_high:
        pm.highest_price = price
    dist = position_manager.trailing_stop_distance_pct(atr, price, float(settings.trailing_stop_percent))
    pm.trailing_stop_price = position_manager.update_trailing_stop(
        pm.trailing_stop_price, float(pm.highest_price), dist
    )

    # 1. Trailing / hard stop -> sell everything. If the gate blocks the sale (manual /
    #    observe / kill switch), keep going so the position still gets reviewed.
    if position_manager.is_trailing_stop_hit(price, float(pm.trailing_stop_price)):
        if _managed_sell_all(db, pm, symbol, "trailing_stop", counts, price):
            return

    # 2. Partial profit ladder (one level per cycle).
    gain = position_manager.gain_pct(float(pm.entry_price), price)
    levels = settings.partial_levels
    lvl = position_manager.next_partial_level(gain, levels, list(pm.partial_levels_taken or []))
    if lvl is not None:
        amounts = settings.partial_amounts
        amt = amounts[levels.index(lvl)] if lvl in levels and len(amounts) > levels.index(lvl) else (
            amounts[0] if amounts else 20)
        qty = position_manager.partial_qty(pm.original_qty, amt)
        # Record the level as taken only if the sell was actually sent.
        level = f"partial_{int(lvl)}pct"
        if _managed_sell_partial(db, pm, symbol, position, qty, level, counts, price):
            pm.partial_levels_taken = list(pm.partial_levels_taken or []) + [lvl]
            pm.last_decision = level
            return

    # 3. Review triggers -> Claude position review.
    prev = _LAST_STATE.get(symbol) or {}
    pnl = position.get("unrealized_plpc")
    secs = (now - pm.last_analyzed_at).total_seconds() if pm.last_analyzed_at else None
    hold_days = (now - pm.opened_at).total_seconds() / 86400 if pm.opened_at else None
    trigs = position_manager.position_review_triggers(
        price=price, prev_price=prev.get("price"), pnl_pct=pnl, prev_pnl_pct=prev.get("pnl_pct"),
        highest_price=float(pm.highest_price), new_high=new_high,
        trailing_stop=float(pm.trailing_stop_price), seconds_since_review=secs,
        recheck_interval=settings.confidence_recheck_interval, hold_days=hold_days,
        max_hold_days=settings.max_hold_days,
    )
    _LAST_STATE[symbol] = {"price": price, "pnl_pct": pnl}
    if not trigs:
        return

    counts["triggers"] += len(trigs)
    audit(db, "monitor_trigger", "monitor", payload={"symbol": symbol, "triggers": trigs, "held": True})
    model_input = _position_payload(symbol, pm, position, mdata, trigs)
    decision, meta, ok = analyst.position_review(model_input)
    counts["claude_calls"] += 1
    if not ok or decision is None:
        counts["errors"] += 1
        audit(db, "monitor_error", "monitor", payload={"symbol": symbol, "error": meta.get("error")})
        return

    conf_pct = decision.confidence * 100
    pm.current_thesis = decision.updated_thesis or pm.current_thesis
    pm.current_confidence = decision.confidence
    pm.exit_condition = decision.exit_condition or pm.exit_condition
    pm.last_decision = decision.action
    pm.last_analyzed_at = now
    audit(db, "monitor_decision", "monitor",
          payload={"symbol": symbol, "action": decision.action, "confidence": round(conf_pct, 1),
                   "reason": decision.reason, "held": True, "prompt_version": meta.get("prompt_version"),
                   "model": meta.get("served_model"), "input": model_input})

    outcome = position_manager.decide_exit(decision.action, conf_pct, float(settings.ai_exit_confidence))
    if outcome == "sell":
        _managed_sell_all(db, pm, symbol, "claude_sell", counts, price)
    elif outcome == "trim":
        amt = settings.partial_amounts[0] if settings.partial_amounts else 20
        qty = position_manager.partial_qty(pm.original_qty, amt)
        _managed_sell_partial(db, pm, symbol, position, qty, "claude_trim", counts, price)
    elif outcome == "tighten_stop":
        tighter = max(position_manager.MIN_TRAIL_PCT, dist / 2)
        pm.trailing_stop_price = position_manager.update_trailing_stop(
            pm.trailing_stop_price, price, tighter
        )
        audit(db, "monitor_stop_adjust", "monitor",
              payload={"symbol": symbol, "kind": "tighten_stop",
                       "new_stop": float(pm.trailing_stop_price)})
    else:  # hold (incl. add / sell-below-confidence) -> no exit
        counts["ignored"] += 1
        audit(db, "monitor_ignored", "monitor",
              payload={"symbol": symbol, "action": decision.action, "confidence": round(conf_pct, 1),
                       "reason": decision.reason})


def _persist_buy_intent(db, run, symbol, decision, snap, mdata, account, held, halted, *,
                        fired=(), data_age_sec: float = 0.0) -> TradeIntent:
    """Persist a monitor-proposed buy. It must pass the same fail-closed output checks as an
    analysis intent (fresh data, limit near market, sane stop/take, evidence) AND the risk
    engine to become pending_approval; otherwise it is stored as rejected_by_risk."""
    max_pct = float(settings.max_position_pct)
    size = clamp_position_size(decision.size_percent or 0, max_pct)
    limit_price = decision.limit_price or snap.get("price") or 0
    intent_dict = {
        "ticker": symbol, "action": "buy", "order_type": "limit", "limit_price": limit_price,
        "position_size_percent": size,
        "stop_loss_percent": decision.stop_loss_percent or DEFAULT_STOP_PCT,
        "take_profit_percent": decision.take_profit_percent or DEFAULT_TAKE_PCT,
        "reason": decision.reason,
        "risks": "(continuous monitor)",
        # The deterministic triggers that prompted the decision are its evidence.
        "evidence": [f"trigger {t.type}: {t.detail}" for t in fired],
    }
    output_violations = intent_validation.validate_intent(
        intent_dict, last_price=(mdata or {}).get("last_price") or snap.get("price"),
        data_age_sec=data_age_sec, max_data_age_sec=settings.max_data_age_sec,
        max_limit_deviation_pct=float(settings.max_limit_deviation_pct),
        quality=(mdata or {}).get("quality"),
    )
    ri = build_risk_input(intent_dict, account, held, halted, mdata)
    res = risk.evaluate(ri)
    passed = res.passed and not output_violations
    violations = output_violations + res.as_violation_dicts()
    row = TradeIntent(
        run_id=run.id, ticker=symbol, action=IntentAction.buy, order_type="limit",
        limit_price=limit_price, position_size_percent=size,
        stop_loss_percent=intent_dict["stop_loss_percent"],
        take_profit_percent=intent_dict["take_profit_percent"],
        confidence=decision.confidence, reason=decision.reason, risks="(continuous monitor)",
        strategy_id="continuous-monitor", evidence=list(intent_dict["evidence"]),
        risk_passed=passed, risk_violations=violations,
        computed_qty=res.computed_qty if passed else None,
        computed_notional=float(res.computed_notional) if passed and res.computed_notional else None,
        status=IntentStatus.pending_approval if passed else IntentStatus.rejected_by_risk,
    )
    db.add(row)
    db.flush()
    return row


def _adjust_protection(db, order, snap, decision, counts, kind: str) -> None:
    price = snap.get("price")
    if order is None or not price:
        counts["ignored"] += 1
        audit(db, "monitor_ignored", "monitor", payload={"reason": "no order/price", "action": kind})
        return
    if kind == "tighten_stop" and decision.stop_loss_percent > 0:
        new_stop = round(price * (1 - decision.stop_loss_percent / 100), 2)
        cur = float(order.stop_loss_price) if order.stop_loss_price else 0
        if new_stop > cur:  # only ever tighten upward
            order.stop_loss_price = new_stop
            audit(db, "monitor_stop_adjust", "monitor",
                  payload={"symbol": order.symbol, "kind": kind, "new_stop": new_stop})
            return
    elif kind == "raise_take_profit" and decision.take_profit_percent > 0:
        new_take = round(price * (1 + decision.take_profit_percent / 100), 2)
        cur = float(order.take_profit_price) if order.take_profit_price else 0
        if order.take_profit_price is None or new_take > cur:
            order.take_profit_price = new_take
            audit(db, "monitor_stop_adjust", "monitor",
                  payload={"symbol": order.symbol, "kind": kind, "new_take": new_take})
            return
    counts["ignored"] += 1
    audit(db, "monitor_ignored", "monitor",
          payload={"symbol": order.symbol, "reason": "no tighter level", "action": kind})


def _update_status(counts: dict) -> None:
    MONITOR_STATUS.update(counts)


def get_status() -> dict:
    cap, reason = continuous_capability()
    return {
        **MONITOR_STATUS,
        "enabled": cap,
        "capability_reason": reason,
        "interval_sec": max(30, min(300, settings.monitor_interval_sec)),
        "is_paper": not settings.is_live,
        "auto_sell_confidence": float(settings.auto_sell_confidence),
        "auto_buy_confidence": float(settings.auto_buy_confidence),
        "max_symbols": settings.monitor_max_symbols,  # 0 = unlimited (all watchlist)
    }


def check_once_safe() -> None:
    db = SessionLocal()
    token = run_id_var.set(f"monitor-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}")
    try:
        # Read-only journal sync; runs in every mode (it never places orders) and a
        # failure here must not stop the monitor cycle.
        order_sync.sync_orders(db)
    except Exception as e:  # noqa: BLE001
        db.rollback()
        log_event(logger, "order_sync.cycle_failed", logging.WARNING, error=safe_error(e, 500))
    try:
        result = check_once(db)
        if not result.get("skipped"):
            log_event(logger, "monitor.cycle", **{k: v for k, v in result.items() if k != "continuous"},
                      **(result.get("continuous") or {}))
    except Exception as e:  # noqa: BLE001
        db.rollback()
        log_event(logger, "monitor.cycle_failed", logging.ERROR, error=safe_error(e, 500))
    finally:
        run_id_var.reset(token)
        db.close()


async def run_loop() -> None:
    interval = max(30, min(300, settings.monitor_interval_sec))
    while True:
        await asyncio.to_thread(check_once_safe)
        await asyncio.sleep(interval)


def start() -> None:
    global _task
    if _task is None:
        _task = asyncio.create_task(run_loop())


def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
