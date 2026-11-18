"""News ingestion: store each story once, flag what is new, and classify it.

- Every article is stored in `news_events` with its source id, publisher, URL, full text
  when the provider sends it, the symbols it mentions, its publication time and when we
  first ingested it.
- Duplicates collapse: the same (source, id) is never stored twice, and the same story
  from another source or for another symbol (same normalized headline on the same day)
  merges into the existing row instead of being shown to the model twice.
- `is_new` is True only the first time a story is ingested, so the watcher and the
  analyst can tell fresh catalysts from ones they have already seen.
- `event_type` is a deterministic keyword classification (CLASSIFIER_VERSION). Model-based
  extraction (confirmed facts, uncertainty, timing) goes into `extraction` in a later phase.

The model sees each item as `news:<id>` so it can cite the exact article as evidence.
News text is DATA — never instructions — wherever it is used.
"""
import hashlib
import re
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import NewsEvent

CLASSIFIER_VERSION = "kw-v1"

# First match wins, so more specific categories come first.
_EVENT_RULES: list[tuple[str, re.Pattern]] = [(name, re.compile(rx, re.I)) for name, rx in [
    ("earnings", r"\b(earnings|eps\b|quarterly results|q[1-4] (results|revenue|sales)"
                 r"|fiscal (q[1-4]|year) results|revenue (beat|miss)|beats? estimates|miss(es)? estimates)"),
    ("guidance", r"\b(guidance|outlook|forecasts?|raises (its )?(full[- ]year|fy|annual))"),
    ("analyst_rating", r"\b(upgrades?|downgrades?|price target|initiates? coverage|reiterates?|"
                       r"overweight|underweight|outperform|underperform)\b"),
    ("m_and_a", r"\b(acquires?|acquisition|merger|to buy|takeover|buyout|tender offer|to be acquired)\b"),
    ("regulatory", r"\b(fda|sec (charges|probe|investigation)|antitrust|doj|ftc|approval|approves|recall)\b"),
    ("legal", r"\b(lawsuit|sues|sued|settlement|class action|court|indictment|verdict)\b"),
    ("offering", r"\b(offering|private placement|convertible notes?|at-the-market|dilution|share sale)\b"),
    ("capital_return", r"\b(dividend|buyback|repurchase)\b"),
    ("management", r"\b(ceo|cfo|chief executive|resigns?|steps down|appoint(s|ed)?|names .* as)\b"),
    ("contract", r"\b(contract|partnership|agreement|awarded|collaboration)\b"),
    ("product", r"\b(launch(es)?|unveils?|introduces?|rolls out)\b"),
    ("macro", r"\b(fed|federal reserve|inflation|cpi|ppi|jobs report|payrolls|tariffs?|interest rates?)\b"),
    ("market_move", r"\b(shares|stock)\b.*\b(rise|rises|fall|falls|jump|jumps|drop|drops|surge|surges|"
                    r"plunge|plunges|soar|soars|slide|slides|moving|trading (higher|lower))\b"),
]]


def classify_event(headline: str, summary: str | None = None) -> str:
    text = f"{headline} {summary or ''}"
    for name, rx in _EVENT_RULES:
        if rx.search(text):
            return name
    return "other"


def _normalize(headline: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (headline or "").lower())).strip()


def dedup_hash(headline: str, published_at: datetime | str | None) -> str:
    """Same normalized headline on the same UTC day = the same story."""
    day = ""
    if isinstance(published_at, datetime):
        day = published_at.date().isoformat()
    elif isinstance(published_at, str):
        day = published_at[:10]
    return hashlib.sha256(f"{_normalize(headline)}|{day}".encode()).hexdigest()


def to_context_item(row: NewsEvent, *, is_new: bool) -> dict:
    """What the model sees for one article (no full text — headline and summary only)."""
    return {
        "id": f"news:{row.id}",
        "headline": row.headline,
        "summary": (row.summary or "")[:400] or None,
        "publisher": row.publisher,
        "published_at": row.published_at.isoformat() if row.published_at else None,
        "event_type": row.event_type,
        "is_new": is_new,
        "symbols": row.symbols,
    }


def ingest_news(db: Session, articles: list[dict], run_id: str | None = None) -> dict[str, list[dict]]:
    """Store articles (deduplicated) and return the model-facing items per symbol.

    Returns {symbol: [context item, ...]} for every symbol the stored stories mention.
    Each story appears once per symbol even if several sources reported it."""
    if not articles:
        return {}
    by_hash: dict[str, dict] = {}
    symbols_by_hash: dict[str, set] = {}
    for a in articles:
        if not a.get("headline"):
            continue
        h = dedup_hash(a["headline"], a.get("published_at"))
        symbols_by_hash.setdefault(h, set()).update(a.get("symbols") or [])
        by_hash.setdefault(h, a)  # first copy of a story wins; later copies are duplicates

    inserted: set[str] = set()
    for h, a in by_hash.items():
        stmt = pg_insert(NewsEvent).values(
            source=a["source"], external_id=a["external_id"], publisher=a.get("publisher"),
            headline=a["headline"], summary=a.get("summary"), content=a.get("content"),
            url=a.get("url"), author=a.get("author"), symbols=sorted(symbols_by_hash[h]),
            published_at=a.get("published_at"), source_updated_at=a.get("source_updated_at"),
            first_seen_run_id=run_id, dedup_hash=h,
            event_type=classify_event(a["headline"], a.get("summary")),
            classifier_version=CLASSIFIER_VERSION,
        ).on_conflict_do_nothing().returning(NewsEvent.id)
        if db.execute(stmt).scalar_one_or_none() is not None:
            inserted.add(h)

    rows = db.scalars(select(NewsEvent).where(or_(
        NewsEvent.dedup_hash.in_(list(by_hash)),
        NewsEvent.external_id.in_([a["external_id"] for a in by_hash.values()]),
    ))).all()
    row_for: dict[str, NewsEvent] = {}
    for h, a in by_hash.items():
        row_for[h] = next((r for r in rows if r.dedup_hash == h), None) or next(
            (r for r in rows if r.source == a["source"] and r.external_id == a["external_id"]), None)

    result: dict[str, list[dict]] = {}
    for h, row in row_for.items():
        if row is None:
            continue
        merged = sorted(set(row.symbols or []) | symbols_by_hash[h])
        if merged != list(row.symbols or []):
            row.symbols = merged  # a known story now also mentions more symbols
        item = to_context_item(row, is_new=h in inserted)
        for s in merged:
            result.setdefault(s, []).append(item)
    db.flush()
    for items in result.values():
        items.sort(key=lambda i: i["published_at"] or "", reverse=True)
    return result
