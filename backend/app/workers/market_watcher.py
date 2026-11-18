"""Haiku market watcher worker.

Run as its own process (needs the schema at revision 0007 and, ideally, the streaming worker):

    WATCHER_ENABLED=true python -m app.workers.market_watcher

Every WATCHER_INTERVAL_SECONDS while the market session is open it:
  1. reads what the streaming worker stored (latest_quotes, 1Min market_bars, news_events);
     only symbols with no fresh stored quote are fetched, in batched snapshot requests;
  2. runs deterministic change detection against the persisted previous state and records
     new `signal_events` (deduplicated);
  3. sends ONLY symbols with a new signal (and out of cooldown) to Haiku, in compact batches,
     skipping the call when the daily/monthly watcher budget would be exceeded;
  4. stores validated decisions, queues INVESTIGATE / REVIEW_POSITION jobs (consumers arrive
     later) and logs + audits ALERTs;
  5. writes a heartbeat.
Outside the session it sleeps longer and makes no Claude calls. Only one instance runs at a
time (PostgreSQL advisory lock). Cycles never overlap.

This worker is read-only toward the broker (positions) and never places, modifies or
cancels orders; Claude has no tools and its output is validated data.
"""
import hashlib
import logging
import signal
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from app.config import settings
from app.deps import audit
from app.logging_config import log_event, run_id_var, safe_error
from app.models import (
    LatestQuote, MarketBar, NewsEvent, PositionManagement, Watchlist, WatcherDecision,
    WatcherSymbolState,
)
from app.services import llm, market_context, market_data, news_store
from app.services.watcher import budget, decide, detect, heartbeat, jobs

logger = logging.getLogger("trading.watcher")

WORKER = "market_watcher"
LOCK_KEY = 7_120_012          # pg advisory lock id (arbitrary, unique to this worker)
ACTOR = "system:watcher"
BAR_WINDOW_MIN = 60
NEWS_LOOKBACK_HOURS = 24
REGIME_REFRESH_SEC = 900
STATE_COLS = ("last_price", "last_price_ts", "last_bar_ts", "regime", "position_fingerprint")


def _utc(ts):
    if isinstance(ts, datetime) and ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def try_acquire_lock(engine):
    """Take the single-instance advisory lock on a dedicated connection, kept open for the
    life of the process. Returns the connection, or None when another instance holds it."""
    conn = engine.connect()
    try:
        got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar()
        conn.commit()
    except Exception:
        conn.close()
        raise
    if not got:
        conn.close()
        return None
    return conn


def release_lock(conn) -> None:
    """Release the advisory lock explicitly (a pooled connection would otherwise keep it)."""
    try:
        conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
        conn.commit()
    finally:
        conn.close()


class MarketWatcher:
    def __init__(self, session_factory=None, *, get_positions=None, clock=None, fetch_snapshots=None,
                 decide_fn=None, regime_fn=None, now=None):
        if session_factory is None:
            from app.database import SessionLocal
            session_factory = SessionLocal
        self.session_factory = session_factory
        self._get_positions = get_positions
        self._clock = clock or market_data.market_clock
        self._fetch_snapshots = fetch_snapshots or market_data.fetch_snapshots
        self._decide = decide_fn or decide.decide
        self._regime_fn = regime_fn or self._compute_regime
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._cycle_lock = threading.Lock()
        self._regime_cache: tuple[float, str | None] | None = None
        self.started_at = self._now()
        self.cycles = 0
        self.stop_event = threading.Event()

    # ------------------------------------------------------------------ heartbeat
    def _beat(self, status: str, details: dict) -> None:
        db = self.session_factory()
        try:
            heartbeat.beat(db, WORKER, status, details, started_at=self.started_at, now=self._now())
            db.commit()
        except Exception as e:  # noqa: BLE001  a heartbeat failure must not kill the worker
            db.rollback()
            log_event(logger, "watcher.heartbeat_failed", logging.WARNING, error=safe_error(e))
        finally:
            db.close()

    # ------------------------------------------------------------------ inputs
    def _compute_regime(self) -> str | None:
        ctx = [s.strip().upper() for s in settings.market_context_symbols.split(",") if s.strip()]
        bars = market_data.fetch_daily_bars(ctx)
        return market_context.compute(bars, ctx).get("regime_hint")

    def _regime(self) -> str | None:
        t = time.monotonic()
        if self._regime_cache and t - self._regime_cache[0] < REGIME_REFRESH_SEC:
            return self._regime_cache[1]
        try:
            value = self._regime_fn()
        except Exception as e:  # noqa: BLE001
            log_event(logger, "watcher.regime_failed", logging.WARNING, error=safe_error(e))
            return self._regime_cache[1] if self._regime_cache else None
        self._regime_cache = (t, value)
        return value

    def _positions(self) -> dict[str, dict]:
        try:
            get = self._get_positions
            if get is None:
                from app.services import broker
                get = broker.get_positions  # read-only
            return {p["symbol"].upper(): p for p in get()}
        except Exception as e:  # noqa: BLE001
            log_event(logger, "watcher.positions_failed", logging.WARNING, error=safe_error(e))
            return {}

    def _load_prices(self, db, symbols: list[str], now: datetime) -> dict[str, dict]:
        """Price per symbol: the stored stream quote when fresh, else a batched snapshot."""
        fresh: dict[str, dict] = {}
        max_age = timedelta(seconds=settings.max_data_age_sec)
        for q in db.scalars(select(LatestQuote).where(LatestQuote.symbol.in_(symbols))):
            ts = _utc(q.last_trade_ts or q.quote_ts)
            price = q.last_trade_price or (round((q.bid + q.ask) / 2, 4) if q.bid and q.ask else None)
            if price and ts and now - ts <= max_age:
                fresh[q.symbol] = {"price": float(price), "ts": ts}
        missing = [s for s in symbols if s not in fresh]
        if missing:
            try:
                for s, snap in self._fetch_snapshots(missing).items():
                    ts = snap.get("last_trade_ts") or snap.get("quote_ts")
                    if snap.get("last_price"):
                        fresh[s] = {"price": float(snap["last_price"]),
                                    "ts": _utc(datetime.fromisoformat(ts)) if ts else now}
            except Exception as e:  # noqa: BLE001
                log_event(logger, "watcher.snapshots_failed", logging.WARNING, error=safe_error(e))
        return fresh

    def _load_bars(self, db, symbols: list[str], now: datetime) -> dict[str, list[dict]]:
        since = now - timedelta(minutes=BAR_WINDOW_MIN)
        out: dict[str, list[dict]] = {s: [] for s in symbols}
        rows = db.execute(select(MarketBar.symbol, MarketBar.ts, MarketBar.high, MarketBar.low,
                                 MarketBar.close, MarketBar.volume)
                          .where(MarketBar.symbol.in_(symbols), MarketBar.timeframe == "1Min",
                                 MarketBar.ts >= since).order_by(MarketBar.ts))
        for sym, ts, hi, lo, cl, vol in rows:
            out[sym].append({"ts": _utc(ts), "high": hi, "low": lo, "close": cl, "volume": vol})
        return out

    def _load_news(self, db, symbols: list[str], states: dict[str, WatcherSymbolState],
                   now: datetime) -> tuple[dict[str, list[dict]], dict[str, int]]:
        """Recent stored news per symbol, with is_new = id above that symbol's last seen id."""
        rows = db.scalars(select(NewsEvent).where(
            NewsEvent.ingested_at >= now - timedelta(hours=NEWS_LOOKBACK_HOURS)
        ).order_by(NewsEvent.id.desc()).limit(500)).all()
        wanted = set(symbols)
        news: dict[str, list[dict]] = {s: [] for s in symbols}
        max_id: dict[str, int] = {}
        for r in rows:
            for s in set(r.symbols or []) & wanted:
                st = states.get(s)
                seen = st.last_news_id if st is not None else None
                item = news_store.to_context_item(r, is_new=seen is not None and r.id > seen)
                item["id_num"] = r.id
                if len(news[s]) < 5:
                    news[s].append(item)
                max_id[s] = max(max_id.get(s, 0), r.id)
        return news, max_id

    # ------------------------------------------------------------------ cycle
    def run_cycle(self) -> dict:
        """One cycle. Returns a summary dict; never raises, never overlaps another cycle."""
        if not self._cycle_lock.acquire(blocking=False):
            log_event(logger, "watcher.cycle_overlap_skipped", logging.WARNING)
            return {"status": "skipped_overlap"}
        try:
            summary = self._cycle()
        except Exception as e:  # noqa: BLE001
            log_event(logger, "watcher.cycle_failed", logging.ERROR, error=safe_error(e))
            summary = {"status": "error", "error": safe_error(e)}
        finally:
            self._cycle_lock.release()
        self.cycles += 1
        summary["cycle"] = self.cycles
        summary["at"] = self._now().isoformat()
        self._beat(summary["status"], {"last_cycle": summary, "enabled": settings.watcher_enabled})
        return summary

    def _cycle(self) -> dict:
        now = self._now()
        clock = self._clock()
        if clock.get("is_open") is not True:  # closed or unknown: no data work, no Claude calls
            return {"status": "idle", "reason": "market_closed" if clock.get("is_open") is False
                    else "clock_unknown"}
        run_id = f"watcher-{now.strftime('%Y%m%dT%H%M%S')}"
        token = run_id_var.set(run_id)
        db = self.session_factory()
        try:
            return self._process(db, now, run_id)
        except Exception:
            db.rollback()
            raise
        finally:
            run_id_var.reset(token)
            db.close()

    def _process(self, db, now: datetime, run_id: str) -> dict:
        wl = {t.upper() for t in db.scalars(select(Watchlist.ticker).where(Watchlist.active.is_(True)))}
        positions = self._positions()
        symbols = sorted(wl | set(positions))
        summary = {"status": "ok", "symbols": len(symbols), "changed": 0, "signals": 0, "calls": 0,
                   "decisions": 0, "jobs": 0, "alerts": 0, "cooldown_skipped": 0, "budget_skipped": 0}
        if not symbols:
            summary["status"] = "idle"
            summary["reason"] = "no_symbols"
            return summary

        states = {s.symbol: s for s in db.scalars(
            select(WatcherSymbolState).where(WatcherSymbolState.symbol.in_(symbols)))}
        prices = self._load_prices(db, symbols, now)
        bars = self._load_bars(db, symbols, now)
        news, max_news = self._load_news(db, symbols, states, now)
        mgmt = {m.symbol: m for m in db.scalars(select(PositionManagement).where(
            PositionManagement.is_open.is_(True), PositionManagement.symbol.in_(list(positions) or [""])))}
        regime = self._regime() if positions else None

        changed: list[dict] = []
        for sym in symbols:
            st = states.get(sym)
            prev = {"initialized": st is not None}
            if st is not None:
                prev.update({c: _utc(getattr(st, c)) if c.endswith("_ts") else getattr(st, c)
                             for c in STATE_COLS})
            pos = None
            if sym in positions:
                m = mgmt.get(sym)
                stop = float(m.trailing_stop_price or m.initial_stop_price or 0) if m else 0
                pos = {"qty": positions[sym].get("qty"), "entry_price": positions[sym].get("avg_entry_price"),
                       "stop": stop or None,
                       "thesis": m.current_thesis if m else None,
                       "confidence": float(m.current_confidence) if m and m.current_confidence else None}
            p = prices.get(sym)
            obs = {"now": now, "price": p["price"] if p else None, "bars": bars.get(sym, []),
                   "news": [{"id": n["id_num"], "headline": n["headline"], "is_new": n["is_new"]}
                            for n in news.get(sym, [])], "position": pos,
                   "regime": regime if sym in positions else None}
            res = detect.detect(sym, obs, prev)
            fresh = detect.persist_signals(db, sym, res.signals, run_id)

            values = {c: res.state.get(c) for c in STATE_COLS}
            if st is None:  # first sight: baseline only, everything stored so far counts as seen
                values["last_news_id"] = max_news.get(sym, 0)
                st = states[sym] = WatcherSymbolState(symbol=sym, **values)
                db.add(st)
            else:
                values["last_news_id"] = max(max_news.get(sym, 0), st.last_news_id or 0)
                for k, v in values.items():
                    setattr(st, k, v)
            if not fresh:
                continue
            summary["signals"] += len(fresh)
            last = st.last_assessed_at if st is not None else None
            if last and now - _utc(last) < timedelta(minutes=settings.watcher_cooldown_minutes):
                summary["cooldown_skipped"] += 1
                log_event(logger, "watcher.cooldown_skip", symbol=sym, signals=len(fresh))
                continue
            changed.append(self._item(sym, fresh, news.get(sym, []), p, pos, st))
        db.commit()
        summary["changed"] = len(changed)

        for i in range(0, len(changed), max(1, settings.watcher_batch_size)):
            batch = changed[i:i + max(1, settings.watcher_batch_size)]
            self._run_batch(db, batch, now, run_id, summary)
        return summary

    @staticmethod
    def _item(sym, fresh, news, price, pos, st) -> dict:
        return {
            "symbol": sym,
            "signals": [{"id": f"signal:{f['id']}", "type": f["event_type"], "at": f["occurred_at"],
                         "detail": f["payload"], "evidence_ids": f["evidence_ids"]} for f in fresh],
            "news": [{"id": n["id"], "headline": n["headline"], "summary": n["summary"],
                      "published_at": n["published_at"], "is_new": n["is_new"]} for n in news],
            "quote": {"price": price["price"], "at": price["ts"].isoformat()} if price else None,
            "position": pos,
            "previous_assessment": {"action": st.last_action,
                                    "at": st.last_assessed_at.isoformat() if st.last_assessed_at else None}
            if st is not None and st.last_assessed_at else None,
        }

    def _run_batch(self, db, batch: list[dict], now: datetime, run_id: str, summary: dict) -> None:
        user_chars = len(decide.build_user_message(batch, now)) + len(decide.system_prompt())
        ok, reason, st = budget.check(db, user_chars, now=now)
        if not ok:
            summary["budget_skipped"] += len(batch)
            summary["budget_skip_reason"] = reason
            summary["budget"] = st.as_dict()
            log_event(logger, "watcher.budget_skip", logging.WARNING, reason=reason, symbols=len(batch),
                      **st.as_dict())
            return
        try:
            decisions, meta = self._decide(batch, now)
        except llm.LLMError as e:
            summary["llm_errors"] = summary.get("llm_errors", 0) + 1
            log_event(logger, "watcher.llm_failed", logging.WARNING, kind=e.kind, detail=e.detail[:200])
            return
        summary["calls"] += 1
        for d in decisions:
            self._store_decision(db, d, meta, run_id, now, summary)
        db.commit()

    def _store_decision(self, db, d: dict, meta: dict, run_id: str, now: datetime, summary: dict) -> None:
        row = WatcherDecision(
            symbol=d["symbol"], action=d["action"], event_type=d["event_type"], urgency=d["urgency"],
            evidence_ids=d["evidence_ids"], explanation=d["explanation"], expires_at=d["expires_at"],
            prompt_version=meta.get("prompt_version"), model=meta.get("served_model"), run_id=run_id,
            signal_event_ids=d["signal_event_ids"], downgraded=d["downgraded"])
        db.add(row)
        db.flush()
        summary["decisions"] += 1
        state = db.get(WatcherSymbolState, d["symbol"])
        if state is not None:
            state.last_assessed_at = now
            state.last_action = d["action"]
        if d["action"] in ("INVESTIGATE", "REVIEW_POSITION"):
            key_src = "|".join(str(i) for i in sorted(d["signal_event_ids"]))
            key = f"{d['action']}:{d['symbol']}:{hashlib.sha256(key_src.encode()).hexdigest()[:16]}"
            job_id = jobs.enqueue(
                db, kind=d["action"], symbol=d["symbol"], job_key=key, decision_id=row.id,
                signal_event_ids=d["signal_event_ids"],
                payload={"urgency": d["urgency"], "event_type": d["event_type"],
                         "evidence_ids": d["evidence_ids"], "explanation": d["explanation"]})
            if job_id is not None:
                summary["jobs"] += 1
        elif d["action"] == "ALERT":
            summary["alerts"] += 1
            log_event(logger, "watcher.alert", logging.WARNING, symbol=d["symbol"], urgency=d["urgency"],
                      event_type=d["event_type"], evidence_ids=d["evidence_ids"],
                      explanation=d["explanation"][:300])
            audit(db, "watcher.alert", ACTOR, payload={
                "symbol": d["symbol"], "urgency": d["urgency"], "event_type": d["event_type"],
                "evidence_ids": d["evidence_ids"], "explanation": d["explanation"], "decision_id": row.id})

    # ------------------------------------------------------------------ loop
    def run_forever(self) -> None:
        while not self.stop_event.is_set():
            summary = self.run_cycle()
            idle = summary.get("status") == "idle"
            wait = settings.watcher_closed_sleep_seconds if idle else settings.watcher_interval_seconds
            self.stop_event.wait(wait)


def main() -> int:
    from app import db_migrate
    from app.database import engine
    from app.logging_config import configure_logging

    configure_logging()
    if not settings.watcher_enabled:
        log_event(logger, "watcher.disabled", logging.WARNING,
                  note="set WATCHER_ENABLED=true to run the market watcher")
        return 0
    status = db_migrate.schema_status()
    if not status["up_to_date"]:
        log_event(logger, "watcher.schema_out_of_date", logging.ERROR, current=status["current"],
                  head=status["head"])
        return 1
    lock_conn = try_acquire_lock(engine)
    if lock_conn is None:
        log_event(logger, "watcher.already_running", logging.ERROR)
        return 1
    watcher = MarketWatcher()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: watcher.stop_event.set())
    log_event(logger, "watcher.start", interval=settings.watcher_interval_seconds,
              model=llm.get_role("watcher").model)
    try:
        watcher.run_forever()
    finally:
        watcher._beat("stopped", {"enabled": settings.watcher_enabled})
        release_lock(lock_conn)
        log_event(logger, "watcher.stop", cycles=watcher.cycles)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
