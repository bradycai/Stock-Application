"""Research-data refresh CLI. Never imported by a request path.

    python -m app.services.research_data.refresh --symbols AAPL,MSFT
    python -m app.services.research_data.refresh --symbols AAPL --skip-macro

Fetches SEC EDGAR company/fundamental/filing data (needs SEC_USER_AGENT) and FRED macro
series (needs FRED_API_KEY) and upserts them. A disabled source is skipped and reported.
"""
import argparse
import logging
import sys
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.logging_config import log_event
from app.services.research_data import store
from app.services.research_data.edgar import EdgarClient, ResearchDataDisabled, safe_fetch
from app.services.research_data.fred import SERIES, FredClient

logger = logging.getLogger("trading.research")


def refresh_edgar(db: Session, symbols: list[str], client: EdgarClient) -> dict:
    """Returns {"enabled": bool, "symbols": {SYMBOL: status}}."""
    if not client.enabled:
        return {"enabled": False, "symbols": {}}
    result: dict[str, str] = {}
    tickers = safe_fetch(client.company_tickers)
    if tickers is None:
        return {"enabled": True, "symbols": {s: "failed: company_tickers" for s in symbols}}
    for symbol in symbols:
        entry = tickers.get(symbol)
        if not entry:
            result[symbol] = "unknown ticker"
            continue
        cik = entry["cik"]
        sub = safe_fetch(client.submissions, cik)
        facts = safe_fetch(client.facts, cik)
        if sub is None and facts is None:
            result[symbol] = "failed"
            continue
        with db.begin_nested():
            if sub is not None:
                store.upsert_company(db, symbol, cik=cik, name=sub["name"] or entry["name"], sic=sub["sic"],
                                     sic_description=sub["sic_description"], exchange=sub["exchange"])
                store.upsert_filings(db, symbol, cik, sub["filings"])
            if facts is not None:
                store.upsert_fundamentals(db, symbol, facts)
        result[symbol] = "ok" if sub is not None and facts is not None else "partial"
    db.commit()
    return {"enabled": True, "symbols": result}


def refresh_fred(db: Session, client: FredClient, *, start: date | None = None,
                 all_vintages: bool = False) -> dict:
    if not client.enabled:
        return {"enabled": False, "series": {}}
    result: dict[str, str] = {}
    for series_id in SERIES:
        rows = safe_fetch(lambda sid=series_id: client.observations(sid, start=start,
                                                                    all_vintages=all_vintages))
        if rows is None:
            result[series_id] = "failed"
            continue
        with db.begin_nested():
            store.upsert_macro(db, rows)
        result[series_id] = f"{len(rows)} rows"
    db.commit()
    return {"enabled": True, "series": result}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Refresh SEC EDGAR fundamentals and FRED macro data.")
    p.add_argument("--symbols", default="", help="comma-separated tickers for EDGAR data")
    p.add_argument("--skip-macro", action="store_true")
    p.add_argument("--all-vintages", action="store_true", help="FRED: store every revision (backtests)")
    p.add_argument("--macro-years", type=int, default=5, help="FRED history window in years")
    args = p.parse_args(argv)
    symbols = list(dict.fromkeys(s.strip().upper() for s in args.symbols.split(",") if s.strip()))

    from app.database import SessionLocal  # deferred: importing creates the engine

    db = SessionLocal()
    try:
        if symbols:
            res = refresh_edgar(db, symbols, EdgarClient())
            if not res["enabled"]:
                print("EDGAR disabled: set SEC_USER_AGENT (descriptive User-Agent with contact details)")
            else:
                for s, status in res["symbols"].items():
                    print(f"{s}: {status}")
        if not args.skip_macro:
            start = date.today() - timedelta(days=365 * args.macro_years)
            res = refresh_fred(db, FredClient(), start=start, all_vintages=args.all_vintages)
            if not res["enabled"]:
                print("FRED disabled: set FRED_API_KEY")
            else:
                for s, status in res["series"].items():
                    print(f"{s}: {status}")
        log_event(logger, "research.refresh_done", symbols=len(symbols))
        return 0
    except ResearchDataDisabled as e:  # pragma: no cover - guarded above
        print(str(e))
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
