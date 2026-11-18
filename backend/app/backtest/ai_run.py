"""CLI: python -m app.backtest.ai_run --start 2026-01-01 --end 2026-06-30 [--split holdout] --out r.json

Replays recorded AI proposals/intents from the database against stored daily bars. Offline: no
Claude, no broker, no network; a missing recorded output is a fail-closed skip. Deterministic
for the same data and seed (`created_at` is the only wall-clock value; pin it with --created-at).
"""
import argparse
import sys
from datetime import date, datetime, timedelta, timezone

from app.backtest import ai_cache, report_io
from app.backtest.ai_replay import ReplayConfig, load_candidates, run_ai_replay, summarize
from app.backtest.data import load_bars_from_db
from app.backtest.engine import CONTEXT_SYMBOLS, BacktestConfig


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.backtest.ai_run", description=__doc__)
    p.add_argument("--start", required=True, type=date.fromisoformat)
    p.add_argument("--end", required=True, type=date.fromisoformat)
    p.add_argument("--split", choices=["none", "holdout", "walk-forward"], default="none")
    p.add_argument("--out", help="write the JSON report here (default: stdout)")
    p.add_argument("--symbols", help="extra comma-separated universe for the momentum baseline")
    p.add_argument("--benchmark", default="SPY")
    p.add_argument("--initial-cash", type=float, default=100_000.0)
    p.add_argument("--slippage-bps", type=float, default=5.0)
    p.add_argument("--fee-bps", type=float, default=0.0)
    p.add_argument("--fee-per-share", type=float, default=0.0)
    p.add_argument("--max-volume-pct", type=float, default=10.0)
    p.add_argument("--default-horizon-days", type=int, default=20)
    p.add_argument("--min-samples", type=int, default=30)
    p.add_argument("--train-days", type=int, default=120)
    p.add_argument("--test-days", type=int, default=60)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--created-at", help="ISO timestamp to stamp the report with (default: now)")
    return p


def main(argv: list[str] | None = None, session=None) -> int:
    args = build_parser().parse_args(argv)
    bc = BacktestConfig(initial_cash=args.initial_cash, benchmark=args.benchmark.upper(),
                        slippage_bps=args.slippage_bps, fee_bps=args.fee_bps,
                        fee_per_share=args.fee_per_share, max_volume_pct=args.max_volume_pct,
                        seed=args.seed)
    cfg = ReplayConfig(backtest=bc, default_horizon_days=args.default_horizon_days,
                       min_samples=args.min_samples)
    own = session is None
    if own:
        from app.database import SessionLocal
        session = SessionLocal()
    try:
        candidates = load_candidates(session, args.start, args.end)
        lo = datetime.combine(args.start, datetime.min.time(), tzinfo=timezone.utc)
        hi = datetime.combine(args.end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        recorded = ai_cache.load_recorded(session, lo, hi)
        extra = {s.strip().upper() for s in (args.symbols or "").split(",") if s.strip()}
        universe = sorted(extra | {c.symbol.upper() for c in candidates})
        # Bars through `end` only: the replay may never see data past the requested window.
        store = load_bars_from_db(session, sorted(set(universe) | {bc.benchmark} | set(CONTEXT_SYMBOLS)),
                                  args.start, args.end)
    finally:
        if own:
            session.close()
    if not candidates:
        print("no recorded proposals or AI intents in the requested range", file=sys.stderr)
        return 2
    try:
        body = run_ai_replay(store, candidates, recorded, args.start, args.end, cfg, args.split,
                             universe, args.train_days, args.test_days)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    report = report_io.envelope("ai_replay", report_io.parse_created_at(args.created_at),
                                summarize(body, args.start, args.end), body)
    report_io.write_report(report, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
