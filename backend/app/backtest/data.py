"""Point-in-time market data.

A `PointInTimeView` is built for one simulated date `t` and contains only bars whose date is
<= t: later bars are not copied into the object, so a strategy cannot read them even by
accident. Asking for an explicit `end` after `t` raises `LeakageError`.
"""
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone


class LeakageError(Exception):
    """A strategy tried to read data that was not yet knowable at the simulated date."""


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def date(self) -> date:
        return self.timestamp.date()


def make_bar(d: date, o: float, h: float, low: float, c: float, v: float = 1_000_000) -> Bar:
    return Bar(datetime(d.year, d.month, d.day, tzinfo=timezone.utc), o, h, low, c, v)


class PointInTimeView:
    __slots__ = ("_as_of", "_bars")

    def __init__(self, as_of: date, bars: dict[str, tuple[Bar, ...]]):
        object.__setattr__(self, "_as_of", as_of)
        object.__setattr__(self, "_bars", bars)

    def __setattr__(self, name, value):
        raise AttributeError("PointInTimeView is immutable")

    @property
    def as_of(self) -> date:
        return self._as_of

    def symbols(self) -> list[str]:
        return sorted(s for s, b in self._bars.items() if b)

    def bars(self, symbol: str, lookback: int | None = None, end: date | None = None) -> list[Bar]:
        if end is not None and end > self._as_of:
            raise LeakageError(f"requested bars through {end} but simulated date is {self._as_of}")
        series = self._bars.get(symbol, ())
        if end is not None:
            series = series[: bisect_right([b.date for b in series], end)]
        if lookback is not None:
            series = series[-lookback:] if lookback > 0 else ()
        return list(series)

    def last_bar(self, symbol: str) -> Bar | None:
        series = self._bars.get(symbol, ())
        return series[-1] if series else None

    def bar_on(self, symbol: str, d: date) -> Bar | None:
        if d > self._as_of:
            raise LeakageError(f"bar for {d} requested at simulated date {self._as_of}")
        for b in reversed(self._bars.get(symbol, ())):
            if b.date == d:
                return b
            if b.date < d:
                break
        return None


class BarStore:
    """All bars for the run. Owned by the engine; strategies only ever see views."""

    def __init__(self, bars: dict[str, list[Bar]]):
        self._bars: dict[str, tuple[Bar, ...]] = {}
        for sym, series in bars.items():
            by_date = {b.date: b for b in sorted(series, key=lambda b: b.timestamp)}
            self._bars[sym] = tuple(by_date[d] for d in sorted(by_date))
        self._dates = {s: [b.date for b in series] for s, series in self._bars.items()}

    def symbols(self) -> list[str]:
        return sorted(self._bars)

    def has(self, symbol: str) -> bool:
        return bool(self._bars.get(symbol))

    def calendar(self, symbols: list[str] | None = None) -> list[date]:
        syms = symbols if symbols is not None else self.symbols()
        return sorted({d for s in syms for d in self._dates.get(s, [])})

    def view(self, as_of: date) -> PointInTimeView:
        cut = {s: series[: bisect_right(self._dates[s], as_of)] for s, series in self._bars.items()}
        return PointInTimeView(as_of, cut)

    def bar_on(self, symbol: str, d: date) -> Bar | None:
        dates = self._dates.get(symbol, [])
        i = bisect_right(dates, d) - 1
        return self._bars[symbol][i] if i >= 0 and dates[i] == d else None

    def summary(self) -> dict:
        return {
            s: {"bars": len(b), "first": b[0].date.isoformat(), "last": b[-1].date.isoformat()}
            for s, b in sorted(self._bars.items()) if b
        }


def load_bars_from_db(session, symbols: list[str], start: date, end: date, warmup_days: int = 420,
                      timeframe: str = "1Day") -> BarStore:
    """Daily bars from `market_bars` (inclusive of `end`, with warm-up history before `start`)."""
    from sqlalchemy import select

    from app.models import MarketBar

    lo = datetime.combine(start - timedelta(days=warmup_days), datetime.min.time(), tzinfo=timezone.utc)
    hi = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    rows = session.execute(
        select(MarketBar)
        .where(MarketBar.symbol.in_(symbols), MarketBar.timeframe == timeframe,
               MarketBar.ts >= lo, MarketBar.ts < hi)
        .order_by(MarketBar.symbol, MarketBar.ts, MarketBar.feed, MarketBar.id)
    ).scalars().all()
    out: dict[str, list[Bar]] = {}
    for r in rows:
        ts = r.ts.astimezone(timezone.utc) if r.ts.tzinfo else r.ts.replace(tzinfo=timezone.utc)
        out.setdefault(r.symbol, []).append(
            Bar(ts, float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume)))
    return BarStore(out)
