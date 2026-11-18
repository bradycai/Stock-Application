"""Research data: EDGAR/FRED parsers, SIC mapping, rate limiter, disabled behavior, upserts,
point-in-time vintages and model-context integration. No network: HTTP is mocked."""
from datetime import date

import httpx
import pytest

from app.config import settings
from app.models import Company, Filing, Fundamental, MacroObservation
from app.services import data_aggregator, market_data
from app.services.research_data import refresh, store
from app.services.research_data.edgar import (
    EdgarClient,
    ResearchDataDisabled,
    parse_companyfacts,
    parse_company_tickers,
    parse_submissions,
)
from app.services.research_data.fred import FredClient, parse_observations
from app.services.research_data.ratelimit import RateLimiter
from app.services.research_data.sectors import sector_from_sic

TICKERS = {"0": {"cik_str": 320193, "ticker": "aapl", "title": "Apple Inc."},
           "1": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"}}

SUBMISSIONS = {
    "cik": "0000320193", "name": "Apple Inc.", "sic": "3571", "sicDescription": "Electronic Computers",
    "exchanges": ["Nasdaq"],
    "filings": {"recent": {
        "accessionNumber": ["0000320193-24-000123", "0000320193-24-000100", "0000320193-24-000090"],
        "form": ["10-K", "4", "8-K"],
        "filingDate": ["2024-11-01", "2024-10-20", "2024-10-31"],
        "reportDate": ["2024-09-28", "", "2024-10-31"],
        "primaryDocument": ["aapl-20240928.htm", "x.xml", "a8k.htm"],
    }},
}


def _f(end, val, form, filed, start, fp="FY"):
    return {"start": start, "end": end, "val": val, "accn": "a", "fy": 2024, "fp": fp, "form": form,
            "filed": filed}


FACTS = {"cik": 320193, "facts": {"us-gaap": {
    "Revenues": {"units": {"USD": [
        _f("2024-09-28", 391e9, "10-K", "2024-11-01", "2023-10-01"),
        _f("2023-09-30", 383e9, "10-K", "2023-11-03", "2022-10-02"),
        _f("2024-06-29", 85e9, "10-Q", "2024-08-02", "2024-03-31", "Q3"),
        _f("2024-06-29", 300e9, "10-Q", "2024-08-02", "2023-10-01", "Q3"),  # year-to-date: dropped
    ]}},
    "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
        _f("2024-09-28", 1.0, "10-K", "2024-11-01", "2023-10-01"),  # lower priority: ignored
    ]}},
    "EarningsPerShareDiluted": {"units": {"USD/shares": [
        _f("2024-09-28", 6.08, "10-K", "2024-11-01", "2023-10-01"),
        _f("2023-09-30", 6.13, "10-K", "2023-11-03", "2022-10-02"),
        _f("2023-09-30", 6.16, "10-K", "2024-11-01", "2022-10-02"),  # restated later: wins
    ]}},
    "NetIncomeLoss": {"units": {"USD": [_f("2024-09-28", 93.7e9, "8-K", "2024-11-01", "2023-10-01")]}},
}}}


# ------------------------------------------------------------------ parsers
def test_parse_company_tickers_pads_cik_and_uppercases():
    out = parse_company_tickers(TICKERS)
    assert out["AAPL"] == {"cik": "0000320193", "name": "Apple Inc."} and "MSFT" in out
    assert parse_company_tickers({"0": {"ticker": "X"}}) == {}


def test_parse_submissions_keeps_only_8k_10q_10k():
    sub = parse_submissions(SUBMISSIONS)
    assert sub["sic"] == "3571" and sub["exchange"] == "Nasdaq" and sub["name"] == "Apple Inc."
    assert [f["form"] for f in sub["filings"]] == ["10-K", "8-K"]
    assert sub["filings"][0]["filed_at"] == date(2024, 11, 1)
    assert sub["filings"][0]["url"].endswith("/320193/000032019324000123/aapl-20240928.htm")
    assert sub["filings"][1]["period_of_report"] == date(2024, 10, 31)


def test_parse_companyfacts_periods_forms_priority_and_restatements():
    rows = parse_companyfacts(FACTS)
    by = {(r["metric"], r["period_end"], r["form"]): r for r in rows}
    assert by[("revenue", date(2024, 9, 28), "10-K")]["value"] == 391e9
    assert by[("revenue", date(2024, 9, 28), "10-K")]["filed_at"] == date(2024, 11, 1)
    assert by[("revenue", date(2024, 6, 29), "10-Q")]["value"] == 85e9  # quarter, not YTD
    assert len([r for r in rows if r["metric"] == "revenue"]) == 3
    assert by[("eps_diluted", date(2023, 9, 30), "10-K")]["value"] == 6.16
    assert not [r for r in rows if r["metric"] == "net_income"]  # 8-K facts ignored
    assert parse_companyfacts({}) == []


def test_parse_fred_observations_missing_value_is_none():
    rows = parse_observations("DGS10", {"observations": [
        {"date": "2024-05-01", "value": "4.55", "realtime_start": "2024-05-02", "realtime_end": "9999-12-31"},
        {"date": "2024-05-02", "value": ".", "realtime_start": "2024-05-03", "realtime_end": "9999-12-31"},
        {"date": "bad", "value": "1", "realtime_start": "2024-05-03"},
    ]})
    assert [r["value"] for r in rows] == [4.55, None]


# ------------------------------------------------------------------ SIC mapping
@pytest.mark.parametrize("sic,sector", [
    ("3571", "Technology"), ("7372", "Technology"), ("3674", "Technology"), ("2834", "Health Care"),
    ("6022", "Financials"), ("1311", "Energy"), ("4911", "Utilities"), ("5411", "Consumer Staples"),
    ("5812", "Consumer Discretionary"), ("2000", "Consumer Staples"), ("3711", "Consumer Discretionary"),
    ("6798", "Financials"), ("6500", "Real Estate"),
])
def test_sic_to_sector(sic, sector):
    assert sector_from_sic(sic)[0] == sector


def test_sic_unknown_or_invalid_is_none():
    assert sector_from_sic(None) == (None, None) and sector_from_sic("abc") == (None, None)
    assert sector_from_sic("9999") == (None, None)


# ------------------------------------------------------------------ rate limiter
def test_rate_limiter_spaces_requests():
    t = [0.0]
    slept = []

    def sleep(d):
        slept.append(d)
        t[0] += d
    lim = RateLimiter(5, clock=lambda: t[0], sleep=sleep)
    for _ in range(6):
        lim.wait()
    assert t[0] == pytest.approx(1.0)  # 6 requests at 5/s need >= 1.0s between first and last
    assert all(d == pytest.approx(0.2) for d in slept)


def test_edgar_client_caps_rate_and_sends_configured_user_agent(monkeypatch):
    monkeypatch.setattr(settings, "sec_max_requests_per_sec", 50.0)
    seen = []

    def handler(request):
        seen.append(request.headers["user-agent"])
        return httpx.Response(200, json=TICKERS)
    c = EdgarClient(user_agent="Test Agent test@example.invalid",
                    http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert c.limiter.interval >= 0.2  # capped at 5/s regardless of the setting
    c.limiter = RateLimiter(1000)
    assert "AAPL" in c.company_tickers() and seen == ["Test Agent test@example.invalid"]


# ------------------------------------------------------------------ disabled behavior
def test_edgar_disabled_without_user_agent_makes_no_request(monkeypatch):
    monkeypatch.setattr(settings, "sec_user_agent", "")

    def boom(request):
        raise AssertionError("network used")
    c = EdgarClient(http=httpx.Client(transport=httpx.MockTransport(boom)))
    assert c.enabled is False
    with pytest.raises(ResearchDataDisabled):
        c.company_tickers()
    assert refresh.refresh_edgar(None, ["AAPL"], c) == {"enabled": False, "symbols": {}}


def test_fred_disabled_without_key(monkeypatch):
    monkeypatch.setattr(settings, "fred_api_key", "")
    c = FredClient()
    assert c.enabled is False
    with pytest.raises(ResearchDataDisabled):
        c.observations("DGS10")
    assert refresh.refresh_fred(None, c) == {"enabled": False, "series": {}}


def test_default_settings_disable_research_sources():
    from app.config import Settings
    s = Settings(_env_file=None)
    assert s.sec_user_agent == "" and s.fred_api_key == ""


# ------------------------------------------------------------------ database
def test_refresh_edgar_upserts_idempotently(pg_db):
    def handler(request):
        url = str(request.url)
        if url.endswith("company_tickers.json"):
            return httpx.Response(200, json=TICKERS)
        if "submissions" in url:
            return httpx.Response(200, json=SUBMISSIONS)
        return httpx.Response(200, json=FACTS)
    client = EdgarClient(user_agent="ua", http=httpx.Client(transport=httpx.MockTransport(handler)),
                         limiter=RateLimiter(1000))
    for _ in range(2):
        res = refresh.refresh_edgar(pg_db, ["AAPL", "ZZZZ"], client)
    assert res["symbols"] == {"AAPL": "ok", "ZZZZ": "unknown ticker"}
    co = pg_db.get(Company, "AAPL")
    assert co.sector == "Technology" and co.cik == "0000320193" and co.exchange == "Nasdaq"
    assert pg_db.query(Fundamental).count() == 5 and pg_db.query(Filing).count() == 2


def test_macro_vintages_are_kept_and_point_in_time(pg_db):
    def row(obs, val, rs):
        return {"series_id": "CPIAUCSL", "observation_date": obs, "value": val, "realtime_start": rs,
                "realtime_end": date(9999, 12, 31)}
    store.upsert_macro(pg_db, [row(date(2024, 4, 1), 100.0, date(2024, 5, 15))])
    store.upsert_macro(pg_db, [row(date(2024, 4, 1), 100.4, date(2024, 6, 15)),  # revision = new vintage
                               row(date(2024, 5, 1), 101.0, date(2024, 6, 15))])
    store.upsert_macro(pg_db, [row(date(2024, 5, 1), 101.0, date(2024, 6, 15))])  # idempotent
    pg_db.commit()
    assert pg_db.query(MacroObservation).count() == 3
    assert store.macro_block(pg_db, as_of=date(2024, 5, 20))["CPIAUCSL"]["value"] == 100.0
    latest = store.macro_block(pg_db)
    assert latest["CPIAUCSL"]["value"] == 101.0 and latest["CPIAUCSL"]["observation_date"] == "2024-05-01"
    assert latest["DGS10"] is None


def test_fundamentals_block_point_in_time_and_yoy(pg_db):
    store.upsert_fundamentals(pg_db, "AAPL", parse_companyfacts(FACTS))
    store.upsert_filings(pg_db, "AAPL", "0000320193", parse_submissions(SUBMISSIONS)["filings"])
    pg_db.commit()
    now = store.fundamentals_block(pg_db, "AAPL")
    assert now["revenue_annual"]["value"] == 391e9 and now["revenue_annual"]["filed_at"] == "2024-11-01"
    assert now["revenue_annual_yoy_growth_pct"] == pytest.approx(2.09, abs=0.01)
    assert now["eps_diluted_annual"]["period_end"] == "2024-09-28"
    assert now["last_filing"]["form"] == "10-K" and now["last_filing"]["filed_at"] == "2024-11-01"
    assert now["revenue_quarterly_yoy_growth_pct"] is None
    # As of mid-2024 the FY2024 10-K had not been filed: only FY2023 is visible.
    past = store.fundamentals_block(pg_db, "AAPL", as_of=date(2024, 6, 1))
    assert past["revenue_annual"]["period_end"] == "2023-09-30" and past["last_filing"] is None
    empty = store.fundamentals_block(pg_db, "NOPE")
    assert empty["revenue_annual"] is None and empty["last_filing"] is None


# ------------------------------------------------------------------ context integration
@pytest.fixture()
def patched_data(monkeypatch):
    monkeypatch.setattr(data_aggregator.broker, "get_account", lambda: {
        "equity": 1e5, "cash": 1e5, "buying_power": 1e5, "day_pnl": 0, "day_pnl_pct": 0, "is_paper": True})
    monkeypatch.setattr(data_aggregator.broker, "get_positions", lambda: [])
    monkeypatch.setattr(market_data, "market_clock", lambda: {"is_open": False})
    monkeypatch.setattr(market_data, "fetch_snapshots", lambda syms: {})
    monkeypatch.setattr(market_data, "fetch_daily_bars", lambda syms: {})
    monkeypatch.setattr(market_data, "fetch_news", lambda syms: [])
    monkeypatch.setattr(settings, "market_context_symbols", "SPY")
    monkeypatch.setattr(settings, "news_api_key", "")


def test_context_reads_stored_research_data_and_flags_missing(pg_db, patched_data):
    store.upsert_company(pg_db, "AAPL", cik="0000320193", name="Apple", sic="3571",
                         sic_description="Electronic Computers", exchange="Nasdaq")
    store.upsert_fundamentals(pg_db, "AAPL", parse_companyfacts(FACTS))
    store.upsert_macro(pg_db, [{"series_id": "DGS10", "observation_date": date(2024, 5, 1), "value": 4.5,
                                "realtime_start": date(2024, 5, 2), "realtime_end": None}])
    pg_db.commit()
    ctx, _ = data_aggregator.build_context(["AAPL", "MSFT"], db=pg_db)
    aapl, msft = ctx["watchlist_data"]["AAPL"], ctx["watchlist_data"]["MSFT"]
    assert aapl["sector"] == "Technology"  # companies table wins over the static map
    assert msft["sector"] == "Technology / Software"  # static fallback
    assert aapl["fundamentals"]["revenue_annual"]["value"] == 391e9
    assert "fundamentals.last_filing" in aapl["quality"]["missing"]  # no filings stored
    assert "fundamentals.revenue_annual" not in aapl["quality"]["missing"]
    assert msft["fundamentals"]["revenue_annual"] is None
    assert "fundamentals.revenue_annual" in msft["quality"]["missing"]
    assert ctx["macro"]["series"]["DGS10"]["value"] == 4.5
    assert ctx["macro"]["series"]["VIXCLS"] is None and "VIXCLS" in ctx["macro"]["missing"]


def test_context_without_db_keeps_old_behavior(patched_data):
    ctx, _ = data_aggregator.build_context(["AAPL"])
    assert ctx["watchlist_data"]["AAPL"]["fundamentals"] == {"earnings_date": None, "analyst_actions": None}
    assert ctx["watchlist_data"]["AAPL"]["sector"] == "Technology / Consumer Hardware"
    assert "macro" not in ctx
