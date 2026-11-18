"""CLI: python -m app.backtest.run --symbols AAPL,MSFT --start 2025-01-01 --end 2026-06-30 --out report.json

Reads daily bars from the `market_bars` table; offline, no broker, no Claude. Output is
deterministic for the same data, config and seed.
"""
import argparse
import json
import sys
from datetime import date

from app.backtest.data import load_bars_from_db
from app.backtest.engine import CONTEXT_SYMBOLS, BacktestConfig
from app.backtest.runner import run_experiment


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.backtest.run", description=__doc__)
    p.add_argument("--symbols", required=True, help="comma-separated universe")
    p.add_argument("--start", required=True, type=date.fromisoformat)
    p.add_argument("--end", required=True, type=date.fromisoformat)
    p.add_argument("--split", choices=["none", "holdout", "walk-forward"], default="none")
    p.add_argument("--out", help="write the JSON report here (default: stdout)")
    p.add_argument("--benchmark", default="SPY")
    p.add_argument("--initial-cash", type=float, default=100_000.0)
    p.add_argument("--top-n", type=int, default=3)
    p.add_argument("--rebalance-days", type=int, default=5)
    p.add_argument("--stop-loss-pct", type=float, default=5.0)
    p.add_argument("--take-profit-pct", type=float, default=10.0)
    p.add_argument("--max-hold-days", type=int, default=20)
    p.add_argument("--slippage-bps", type=float, default=5.0)
    p.add_argument("--fee-bps", type=float, default=0.0)
    p.add_argument("--fee-per-share", type=float, default=0.0)
    p.add_argument("--max-volume-pct", type=float, default=10.0)
    p.add_argument("--train-days", type=int, default=120)
    p.add_argument("--test-days", type=int, default=60)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--created-at", help="ISO timestamp: stamp the report with kind/created_at/summary "
                                        "so the reports API lists it (omitted: output unchanged)")
    return p


def main(argv: list[str] | None = None, session=None) -> int:
    args = build_parser().parse_args(argv)
    universe = sorted({s.strip().upper() for s in args.symbols.split(",") if s.strip()})
    config = BacktestConfig(
        initial_cash=args.initial_cash, benchmark=args.benchmark.upper(), top_n=args.top_n,
        rebalance_days=args.rebalance_days, stop_loss_pct=args.stop_loss_pct,
        take_profit_pct=args.take_profit_pct, max_hold_days=args.max_hold_days,
        slippage_bps=args.slippage_bps, fee_bps=args.fee_bps, fee_per_share=args.fee_per_share,
        max_volume_pct=args.max_volume_pct, seed=args.seed)
    own_session = session is None
    if own_session:
        from app.database import SessionLocal
        session = SessionLocal()
    try:
        load = sorted(set(universe) | {config.benchmark} | set(CONTEXT_SYMBOLS))
        store = load_bars_from_db(session, load, args.start, args.end)
    finally:
        if own_session:
            session.close()
    if not any(store.has(s) for s in universe):
        print("no market_bars found for the requested symbols", file=sys.stderr)
        return 2
    if not store.has(config.benchmark):
        print(f"warning: no bars for benchmark {config.benchmark}; benchmark return will be 0",
              file=sys.stderr)
    try:
        report = run_experiment(store, universe, args.start, args.end, config, args.split,
                                train_days=args.train_days, test_days=args.test_days)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.created_at:
        from app.backtest import report_io
        report = report_io.envelope("baseline", report_io.parse_created_at(args.created_at),
                                    report_io.baseline_summary(report, args.start, args.end), report)
    text = json.dumps(report, indent=2, sort_keys=True, default=str)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
