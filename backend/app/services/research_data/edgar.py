"""SEC EDGAR client + pure parsers.

SEC requires a descriptive User-Agent with contact details; it comes from the
`sec_user_agent` setting (never hard-coded). With it empty, EDGAR is disabled and every
fetch raises `ResearchDataDisabled` (callers label the fields missing). Requests are
rate limited client-side to <= 5/s.
"""
import logging
from datetime import date

import httpx

from app.config import settings
from app.logging_config import log_event, safe_error
from app.services.research_data.ratelimit import RateLimiter

logger = logging.getLogger("trading.research")

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"

FILING_FORMS = {"8-K", "10-Q", "10-K"}
MAX_RATE = 5.0

# metric -> (unit, candidate us-gaap concepts in priority order)
METRICS: dict[str, tuple[str, list[str]]] = {
    "revenue": ("USD", ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                        "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax"]),
    "net_income": ("USD", ["NetIncomeLoss", "ProfitLoss"]),
    "eps_diluted": ("USD/shares", ["EarningsPerShareDiluted"]),
    "gross_profit": ("USD", ["GrossProfit"]),
    "operating_income": ("USD", ["OperatingIncomeLoss"]),
}


class ResearchDataDisabled(RuntimeError):
    """The data source is not configured (missing User-Agent / API key)."""


def _date(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def pad_cik(cik) -> str:
    return str(int(cik)).zfill(10)


# ---------------------------------------------------------------- pure parsers

def parse_company_tickers(data: dict) -> dict[str, dict]:
    """company_tickers.json -> {SYMBOL: {cik (10 digits), name}}."""
    out: dict[str, dict] = {}
    rows = data.values() if isinstance(data, dict) else data
    for row in rows or []:
        try:
            out[str(row["ticker"]).upper()] = {"cik": pad_cik(row["cik_str"]), "name": row.get("title")}
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _at(col, i):
    try:
        return (col or [])[i]
    except IndexError:
        return None


def parse_submissions(data: dict, limit: int = 20) -> dict:
    """submissions JSON -> {name, sic, sic_description, exchange, filings: [recent 8-K/10-Q/10-K]}."""
    cik = pad_cik(data.get("cik") or 0)
    exchanges = data.get("exchanges") or []
    recent = (data.get("filings") or {}).get("recent") or {}
    filings = []
    for i, form in enumerate(recent.get("form") or []):
        acc = _at(recent.get("accessionNumber"), i)
        if form not in FILING_FORMS or not acc:
            continue
        doc = _at(recent.get("primaryDocument"), i)
        filings.append({
            "accession": acc, "form": form,
            "filed_at": _date(_at(recent.get("filingDate"), i)),
            "period_of_report": _date(_at(recent.get("reportDate"), i)),
            "url": ARCHIVE_URL.format(cik=int(cik), acc=acc.replace("-", ""), doc=doc) if doc else None,
        })
        if len(filings) >= limit:
            break
    return {
        "name": data.get("name"),
        "sic": str(data["sic"]) if data.get("sic") else None,
        "sic_description": data.get("sicDescription") or None,
        "exchange": exchanges[0] if exchanges else None,
        "filings": filings,
    }


def _duration_days(fact: dict) -> int | None:
    start, end = _date(fact.get("start")), _date(fact.get("end"))
    return (end - start).days if start and end else None


def parse_companyfacts(data: dict) -> list[dict]:
    """companyfacts XBRL -> normalized fact rows.

    Keeps 10-K full-year facts (~1 year) and 10-Q single-quarter facts (~3 months); drops
    year-to-date and other durations. One row per (metric, period_end, form): the first
    concept in priority order wins, and a later-filed restatement replaces an earlier one.
    """
    gaap = (data.get("facts") or {}).get("us-gaap") or {}
    cik = pad_cik(data["cik"]) if data.get("cik") is not None else None
    out: list[dict] = []
    for metric, (unit, concepts) in METRICS.items():
        claimed: set[tuple] = set()  # (period_end, form) taken by a higher-priority concept
        for concept in concepts:
            facts = ((gaap.get(concept) or {}).get("units") or {}).get(unit) or []
            taken: dict[tuple, dict] = {}
            for f in facts:
                form, end, val = f.get("form"), _date(f.get("end")), f.get("val")
                if form not in ("10-K", "10-Q") or end is None or isinstance(val, bool) \
                        or not isinstance(val, (int, float)):
                    continue
                days = _duration_days(f)
                if days is None or not ((form == "10-K" and 330 <= days <= 400)
                                        or (form == "10-Q" and 70 <= days <= 110)):
                    continue
                key = (end, form)
                if key in claimed:
                    continue
                filed = _date(f.get("filed"))
                prev = taken.get(key)
                if prev and (prev["filed_at"] or date.min) >= (filed or date.min):
                    continue
                taken[key] = {"metric": metric, "cik": cik, "period_end": end,
                              "fiscal_period": f.get("fp"), "form": form, "value": float(val),
                              "unit": unit, "filed_at": filed}
            claimed |= set(taken)
            out.extend(taken.values())
    return sorted(out, key=lambda r: (r["metric"], r["period_end"], r["form"]))


# ---------------------------------------------------------------- client

class EdgarClient:
    def __init__(self, *, user_agent: str | None = None, http: httpx.Client | None = None,
                 limiter: RateLimiter | None = None):
        self.user_agent = (settings.sec_user_agent if user_agent is None else user_agent).strip()
        self._http = http
        self.limiter = limiter or RateLimiter(min(settings.sec_max_requests_per_sec, MAX_RATE))

    @property
    def enabled(self) -> bool:
        return bool(self.user_agent)

    def _get(self, url: str) -> dict:
        if not self.enabled:
            raise ResearchDataDisabled("SEC_USER_AGENT is not set; EDGAR is disabled")
        self.limiter.wait()
        resp = (self._http or httpx).get(
            url, headers={"User-Agent": self.user_agent, "Accept": "application/json"},
            timeout=settings.research_http_timeout_sec)
        resp.raise_for_status()
        return resp.json()

    def company_tickers(self) -> dict[str, dict]:
        return parse_company_tickers(self._get(TICKERS_URL))

    def submissions(self, cik: str) -> dict:
        return parse_submissions(self._get(SUBMISSIONS_URL.format(cik=pad_cik(cik))))

    def facts(self, cik: str) -> list[dict]:
        return parse_companyfacts(self._get(FACTS_URL.format(cik=pad_cik(cik))))


def safe_fetch(fn, *args):
    """Run a fetch; returns None (and logs) on any HTTP/parse error, re-raises 'disabled'."""
    try:
        return fn(*args)
    except ResearchDataDisabled:
        raise
    except Exception as e:  # noqa: BLE001
        log_event(logger, "research.fetch_failed", logging.WARNING, error=safe_error(e))
        return None
