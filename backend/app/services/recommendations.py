"""Pure helpers for post-processing Claude's recommendations. No heavy imports so this
stays trivially unit-testable."""


def _conf(obj) -> float:
    return float(getattr(obj, "confidence", 0) or 0)


def _ticker(obj) -> str:
    return (getattr(obj, "ticker", "") or "").upper()


def clamp_position_size(size_pct, max_pct: float) -> float:
    """Clamp a proposed position size to the configured maximum.

    Claude is told the cap but occasionally proposes a larger size; this is the
    deterministic safeguard so an over-size BUY is shrunk to the max rather than rejected.
    Never increases a size and never goes below 0.
    """
    if size_pct is None:
        return 0.0
    return max(0.0, min(float(size_pct), float(max_pct)))


def dedupe_by_ticker(intents: list) -> list:
    """Collapse duplicate tickers to a single intent each.

    Claude occasionally emits the same ticker more than once in a run. We keep one intent
    per ticker — the highest-confidence one — and preserve first-appearance order for a
    stable display. Works on Pydantic models or any object with .ticker/.confidence.
    """
    best: dict[str, object] = {}
    order: list[str] = []
    for it in intents:
        t = _ticker(it)
        if not t:
            continue
        if t not in best:
            best[t] = it
            order.append(t)
        elif _conf(it) > _conf(best[t]):
            best[t] = it
    return [best[t] for t in order]
