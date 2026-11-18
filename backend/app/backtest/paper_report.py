"""Paper experiment report: what the fill model predicted vs what the paper account did.

CLI: python -m app.backtest.paper_report --start 2026-01-01 --end 2026-06-30 --out paper.json

Read-only: it reads `orders` / `order_fills` and stored daily bars from the database. It never
talks to the broker, Claude or any network service, and never writes to the database.

For each paper order the existing fill model (fills.py) predicts, from the daily bar of the
submit date (or the next trading bar), whether the order fills and at what price. Slippage is
signed in bps, positive = worse for the trader, measured against the limit price for limit orders
and the bar open for market orders. Bracket legs are not modelled (entry orders only).
"""
import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from statistics import mean, median

from app.backtest import report_io
from app.backtest.data import Bar, BarStore, load_bars_from_db
from app.backtest.fills import (
    CostModel,
    fill_buy_limit,
    fill_market_at_open,
    fill_sell_limit,
)


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def adverse_bps(side: str, price: float, ref: float) -> float:
    """Signed slippage in bps; positive = the trader did worse than `ref`."""
    if ref <= 0:
        return 0.0
    move = (price / ref - 1) * 10_000
    return move if side == "buy" else -move


def predict(order: dict, bar: Bar | None, cost: CostModel) -> dict:
    """Model prediction for one order: {"status", "filled", "price", "ref", "slippage_bps"}."""
    if bar is None:
        return {"status": "no_bars", "filled": None, "price": None, "ref": None, "slippage_bps": None}
    side, qty, otype = order["side"].lower(), int(order["qty"]), order["order_type"].lower()
    if otype == "limit" and order.get("limit_price"):
        limit = float(order["limit_price"])
        fill = (fill_buy_limit(limit, qty, bar, cost) if side == "buy"
                else fill_sell_limit(limit, qty, bar, cost))
        ref = limit
    elif otype == "market":
        fill = fill_market_at_open(side, qty, bar, cost, "market")
        ref = bar.open
    else:
        return {"status": "unsupported_order_type", "filled": None, "price": None, "ref": None,
                "slippage_bps": None}
    if fill is None:
        return {"status": "ok", "filled": False, "price": None, "ref": ref, "slippage_bps": None}
    return {"status": "ok", "filled": True, "price": round(fill.price, 6), "ref": ref,
            "qty": fill.qty, "slippage_bps": round(adverse_bps(side, fill.price, ref), 4)}


def compare_order(order: dict, store: BarStore, cost: CostModel) -> dict:
    submitted = _utc(order["submitted_at"])
    bars = store.bars_after(order["symbol"], submitted.date() - timedelta(days=1))
    bar = bars[0] if bars else None
    pred = predict(order, bar, cost)
    filled = int(order.get("filled_qty") or 0) > 0
    actual_price = float(order["filled_avg_price"]) if filled and order.get("filled_avg_price") else None
    ref = pred["ref"]
    actual_bps = (round(adverse_bps(order["side"].lower(), actual_price, ref), 4)
                  if actual_price is not None and ref else None)
    fills = sorted(_utc(t) for t, q in order.get("fills", []) if t is not None and (q or 0) > 0)
    latency = (fills[0] - submitted).total_seconds() if fills else None
    out = {
        "order_id": order["id"], "symbol": order["symbol"], "side": order["side"],
        "order_type": order["order_type"], "qty": int(order["qty"]),
        "limit_price": float(order["limit_price"]) if order.get("limit_price") else None,
        "status": order["status"], "submitted_at": submitted.isoformat(),
        "model_bar_date": bar.date.isoformat() if bar else None, "prediction": pred,
        "actual": {"filled": filled, "filled_qty": int(order.get("filled_qty") or 0),
                   "price": actual_price, "slippage_bps": actual_bps, "latency_sec": latency},
    }
    if pred["filled"] is not None and filled and pred["price"] is not None and actual_price is not None:
        out["price_error_bps"] = round(adverse_bps(order["side"].lower(), actual_price, pred["price"]), 4)
    return out


def aggregate(rows: list[dict]) -> dict:
    comparable = [r for r in rows if r["prediction"]["filled"] is not None]
    n = len(comparable)
    actual_f = [r for r in comparable if r["actual"]["filled"]]
    pred_f = [r for r in comparable if r["prediction"]["filled"]]
    both = [r for r in comparable if r["actual"]["filled"] and r["prediction"]["filled"]]
    bias_pairs = [(r["actual"]["slippage_bps"], r["prediction"]["slippage_bps"]) for r in both
                  if r["actual"]["slippage_bps"] is not None]
    lat = [r["actual"]["latency_sec"] for r in rows if r["actual"]["latency_sec"] is not None]
    a_rate, p_rate = (len(actual_f) / n, len(pred_f) / n) if n else (None, None)
    return {
        "orders": len(rows), "comparable_orders": n,
        "not_comparable": len(rows) - n,
        "actual_fill_rate": a_rate, "predicted_fill_rate": p_rate,
        "fill_rate_diff": None if n == 0 else a_rate - p_rate,
        "agreement": {"both_filled": len(both),
                      "model_only_filled": len(pred_f) - len(both),
                      "actual_only_filled": len(actual_f) - len(both),
                      "neither_filled": n - len(actual_f) - len(pred_f) + len(both)},
        "slippage": {
            "n": len(bias_pairs),
            "mean_actual_bps": mean(a for a, _ in bias_pairs) if bias_pairs else None,
            "mean_predicted_bps": mean(p for _, p in bias_pairs) if bias_pairs else None,
            "bias_bps": mean(a - p for a, p in bias_pairs) if bias_pairs else None,
        },
        "latency_sec": {"n": len(lat), "mean": mean(lat) if lat else None,
                        "median": median(lat) if lat else None, "max": max(lat) if lat else None},
    }


def build_paper_report(orders: list[dict], store: BarStore, cost: CostModel) -> dict:
    rows = [compare_order(o, store, cost) for o in sorted(orders, key=lambda o: o["id"])]
    return {"cost_model": {"slippage_bps": cost.slippage_bps, "fee_bps": cost.fee_bps,
                           "fee_per_share": cost.fee_per_share, "max_volume_pct": cost.max_volume_pct},
            "aggregate": aggregate(rows), "orders": rows,
            "notes": ["Bracket legs are not modelled; each order is compared on its entry leg.",
                      "Market-order slippage is vs the bar open, so it includes the intraday move "
                      "between the open and the submit time."]}


def summarize(body: dict, start: date, end: date) -> dict:
    a = body["aggregate"]
    return {"start": start.isoformat(), "end": end.isoformat(), "orders": a["orders"],
            "comparable_orders": a["comparable_orders"], "actual_fill_rate": a["actual_fill_rate"],
            "predicted_fill_rate": a["predicted_fill_rate"], "fill_rate_diff": a["fill_rate_diff"],
            "slippage_bias_bps": a["slippage"]["bias_bps"]}


def load_orders(db, start: date, end: date, include_live: bool = False) -> list[dict]:
    from sqlalchemy import select

    from app.models import Order, OrderFill

    lo = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
    hi = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    stmt = select(Order).where(Order.submitted_at >= lo, Order.submitted_at < hi).order_by(Order.id)
    if not include_live:
        stmt = stmt.where(Order.is_live.is_(False))  # the paper experiment only
    orders = list(db.scalars(stmt))
    fills: dict[int, list] = {}
    if orders:
        for f in db.scalars(select(OrderFill).where(OrderFill.order_id.in_([o.id for o in orders]))
                            .order_by(OrderFill.id)):
            fills.setdefault(f.order_id, []).append((f.filled_at, f.filled_qty))
    return [{"id": o.id, "symbol": o.symbol.upper(), "side": o.side, "qty": o.qty,
             "order_type": o.order_type, "limit_price": o.limit_price, "submitted_at": o.submitted_at,
             "status": o.status, "filled_qty": o.filled_qty, "filled_avg_price": o.filled_avg_price,
             "fills": fills.get(o.id, [])} for o in orders]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.backtest.paper_report", description=__doc__)
    p.add_argument("--start", required=True, type=date.fromisoformat)
    p.add_argument("--end", required=True, type=date.fromisoformat)
    p.add_argument("--out", help="write the JSON report here (default: stdout)")
    p.add_argument("--slippage-bps", type=float, default=5.0)
    p.add_argument("--fee-bps", type=float, default=0.0)
    p.add_argument("--fee-per-share", type=float, default=0.0)
    p.add_argument("--max-volume-pct", type=float, default=10.0)
    p.add_argument("--created-at", help="ISO timestamp to stamp the report with (default: now)")
    return p


def main(argv: list[str] | None = None, session=None) -> int:
    args = build_parser().parse_args(argv)
    cost = CostModel(args.slippage_bps, args.fee_bps, args.fee_per_share, args.max_volume_pct)
    own = session is None
    if own:
        from app.database import SessionLocal
        session = SessionLocal()
    try:
        orders = load_orders(session, args.start, args.end)
        symbols = sorted({o["symbol"] for o in orders})
        # Bars a few days past `end` so an order submitted on the last day can still be compared.
        store = (load_bars_from_db(session, symbols, args.start, args.end + timedelta(days=5),
                                   warmup_days=5) if symbols else BarStore({}))
    finally:
        if own:
            session.close()
    body = build_paper_report(orders, store, cost)
    body["window"] = {"start": args.start.isoformat(), "end": args.end.isoformat()}
    report = report_io.envelope("paper_report", report_io.parse_created_at(args.created_at),
                                summarize(body, args.start, args.end), body)
    report_io.write_report(report, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
