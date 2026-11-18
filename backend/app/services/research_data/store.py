"""Database upserts and reads for research data. No network here; reads feed the model context."""
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import Company, Filing, Fundamental, MacroObservation
from app.services.research_data.fred import SERIES
from app.services.research_data.sectors import sector_from_sic


# ---------------------------------------------------------------- writes

def upsert_company(db: Session, symbol: str, *, cik: str | None, name: str | None, sic: str | None,
                   sic_description: str | None, exchange: str | None) -> None:
    sector, industry = sector_from_sic(sic)
    vals = {"symbol": symbol.upper(), "cik": cik, "name": name, "sic": sic,
            "sic_description": sic_description, "sector": sector,
            "industry": industry or sic_description, "exchange": exchange}
    stmt = insert(Company).values(**vals)
    db.execute(stmt.on_conflict_do_update(
        index_elements=[Company.symbol],
        set_={**{k: v for k, v in vals.items() if k != "symbol"}, "updated_at": func.now()}))


def upsert_fundamentals(db: Session, symbol: str, rows: list[dict]) -> int:
    for r in rows:
        vals = {**r, "symbol": symbol.upper(), "source": "sec_edgar"}
        stmt = insert(Fundamental).values(**vals)
        db.execute(stmt.on_conflict_do_update(
            constraint="uq_fundamentals_key",
            set_={k: vals[k] for k in ("cik", "fiscal_period", "value", "unit", "filed_at", "source")}))
    return len(rows)


def upsert_filings(db: Session, symbol: str, cik: str | None, rows: list[dict]) -> int:
    for r in rows:
        vals = {**r, "symbol": symbol.upper(), "cik": cik}
        stmt = insert(Filing).values(**vals)
        db.execute(stmt.on_conflict_do_update(
            index_elements=[Filing.accession],
            set_={k: vals[k] for k in ("form", "filed_at", "period_of_report", "url")}))
    return len(rows)


def upsert_macro(db: Session, rows: list[dict]) -> int:
    """Insert observations; a new vintage (realtime_start) is a new row, never an overwrite."""
    for r in rows:
        vals = {**r, "source": "fred"}
        stmt = insert(MacroObservation).values(**vals)
        db.execute(stmt.on_conflict_do_update(
            constraint="uq_macro_obs_key",
            set_={"value": vals["value"], "realtime_end": vals["realtime_end"]}))
    return len(rows)


# ---------------------------------------------------------------- reads

def company_sector(db: Session, symbol: str) -> str | None:
    row = db.get(Company, symbol.upper())
    return row.sector if row and row.sector else None


def _fact(r: Fundamental) -> dict:
    return {"value": r.value, "unit": r.unit, "period_end": r.period_end.isoformat(),
            "fiscal_period": r.fiscal_period, "form": r.form,
            "filed_at": r.filed_at.isoformat() if r.filed_at else None}


def _latest(db: Session, symbol: str, metric: str, form: str, as_of: date | None) -> list[Fundamental]:
    q = select(Fundamental).where(Fundamental.symbol == symbol, Fundamental.metric == metric,
                                  Fundamental.form == form)
    if as_of:
        q = q.where(Fundamental.filed_at <= as_of)
    return list(db.scalars(q.order_by(Fundamental.period_end.desc()).limit(8)))


def _yoy(rows: list[Fundamental]) -> float | None:
    """Growth vs. the fact whose period ended ~1 year before the latest one."""
    if not rows:
        return None
    cur = rows[0]
    for prev in rows[1:]:
        if 350 <= (cur.period_end - prev.period_end).days <= 380:
            return round((cur.value - prev.value) / abs(prev.value) * 100, 2) if prev.value else None
    return None


def fundamentals_block(db: Session, symbol: str, as_of: date | None = None) -> dict:
    """Stored fundamentals for the model context. Absent values stay None (listed as missing).

    `as_of` limits to facts filed on/before that date (point-in-time)."""
    symbol = symbol.upper()
    out: dict = {}
    for label, metric, form in (("revenue_annual", "revenue", "10-K"),
                                ("revenue_quarterly", "revenue", "10-Q"),
                                ("eps_diluted_annual", "eps_diluted", "10-K"),
                                ("eps_diluted_quarterly", "eps_diluted", "10-Q")):
        rows = _latest(db, symbol, metric, form, as_of)
        out[label] = _fact(rows[0]) if rows else None
        if metric == "revenue":
            out[label + "_yoy_growth_pct"] = _yoy(rows)
    q = select(Filing).where(Filing.symbol == symbol, Filing.form.in_(["10-K", "10-Q"]))
    if as_of:
        q = q.where(Filing.filed_at <= as_of)
    f = db.scalars(q.order_by(Filing.filed_at.desc()).limit(1)).first()
    out["last_filing"] = ({"form": f.form, "filed_at": f.filed_at.isoformat() if f.filed_at else None,
                           "period_of_report": f.period_of_report.isoformat() if f.period_of_report else None}
                          if f else None)
    return out


def macro_block(db: Session, as_of: date | None = None) -> dict:
    """Latest non-null value per FRED series (latest vintage known by `as_of`); None when absent."""
    out: dict = {}
    for series_id in SERIES:
        q = select(MacroObservation).where(MacroObservation.series_id == series_id,
                                           MacroObservation.value.is_not(None))
        if as_of:
            q = q.where(MacroObservation.realtime_start <= as_of)
        r = db.scalars(q.order_by(MacroObservation.observation_date.desc(),
                                  MacroObservation.realtime_start.desc()).limit(1)).first()
        out[series_id] = ({"value": r.value, "observation_date": r.observation_date.isoformat(),
                           "realtime_start": r.realtime_start.isoformat()} if r else None)
    return out
