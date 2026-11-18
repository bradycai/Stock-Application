"""FRED client + pure parser. Disabled (raises ResearchDataDisabled) without `fred_api_key`."""
from datetime import date

import httpx

from app.config import settings
from app.services.research_data.edgar import ResearchDataDisabled

OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
SERIES = {
    "DGS10": "10-year Treasury yield (%)",
    "DGS2": "2-year Treasury yield (%)",
    "FEDFUNDS": "Effective federal funds rate (%)",
    "CPIAUCSL": "CPI, all urban consumers (index)",
    "UNRATE": "Unemployment rate (%)",
    "VIXCLS": "CBOE VIX close",
}


def _d(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def parse_observations(series_id: str, data: dict) -> list[dict]:
    """FRED observations JSON -> rows. '.' (missing) values are kept as value=None."""
    out = []
    for o in data.get("observations") or []:
        d, rs = _d(o.get("date")), _d(o.get("realtime_start"))
        if d is None or rs is None:
            continue
        try:
            val = float(o.get("value"))
        except (TypeError, ValueError):
            val = None
        out.append({"series_id": series_id, "observation_date": d, "value": val,
                    "realtime_start": rs, "realtime_end": _d(o.get("realtime_end"))})
    return out


class FredClient:
    def __init__(self, *, api_key: str | None = None, http: httpx.Client | None = None):
        self.api_key = (settings.fred_api_key if api_key is None else api_key).strip()
        self._http = http

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def observations(self, series_id: str, *, start: date | None = None,
                     all_vintages: bool = False) -> list[dict]:
        """Observations for a series. all_vintages=True requests every revision (for backtests)."""
        if not self.enabled:
            raise ResearchDataDisabled("FRED_API_KEY is not set; FRED is disabled")
        params = {"series_id": series_id, "api_key": self.api_key, "file_type": "json"}
        if start:
            params["observation_start"] = start.isoformat()
        if all_vintages:
            params.update(realtime_start="1776-07-04", realtime_end="9999-12-31")
        resp = (self._http or httpx).get(OBSERVATIONS_URL, params=params,
                                         timeout=settings.research_http_timeout_sec)
        resp.raise_for_status()
        return parse_observations(series_id, resp.json())
