"""CLI: python -m app.services.review.run --date 2026-11-17 [--weekly]

Builds the daily review (and the weekly rollup with --weekly) from the journal in the
database. Offline and deterministic unless REVIEW_LLM_SUMMARY=true. Re-running replaces that
period's report and strategy metrics."""
import argparse
import sys
from datetime import date

from app.database import SessionLocal
from app.services.review import daily


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.services.review.run", description=__doc__)
    p.add_argument("--date", required=True, type=date.fromisoformat, help="UTC day to review")
    p.add_argument("--weekly", action="store_true", help="also build the 7-day rollup ending that day")
    args = p.parse_args(argv)
    db = SessionLocal()
    try:
        reports = daily.run_review(db, args.date, weekly=args.weekly)
        for r in reports:
            print(f"{r.kind} report id={r.id}: {r.summary.splitlines()[0]}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
