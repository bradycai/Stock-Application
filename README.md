# AI Trading Assistant

[![CI](https://github.com/bradycai/Stock-Application/actions/workflows/ci.yml/badge.svg?branch=Production)](https://github.com/bradycai/Stock-Application/actions/workflows/ci.yml)

A full-stack trading assistant that connects Claude to an Alpaca brokerage account
through a controlled backend. **Claude never places trades.** It analyzes market data
and returns structured JSON trade *intents*. A deterministic risk engine validates every
intent, a human approves each one (unless automation is explicitly set to Auto), and only
then does the backend submit an order to Alpaca.

> This is an engineering project, not a promise of profitability or a recommendation to
> trade real money. Keep `TRADING_MODE=paper` until the system has been independently
> validated.

```
React dashboard ──▶ FastAPI backend ──▶ Alpaca (paper → live)
                         │   │  └────▶ PostgreSQL (audit / intents / orders)
                         │   └───────▶ Claude API   (analysis only, no order tools)
                         └───────────▶ Risk engine  (validates every intent)
```

## Safety model

- Claude has **no broker credentials and no order tool** — it physically cannot trade.
- Every Claude request goes through one client (`services/llm.py`) that uses **structured
  outputs**, validates the response again with Pydantic, and **fails closed** on refusals,
  truncated responses, API errors or invalid output.
- Every intent is then checked for stale data, an off-market limit price and missing
  evidence, **risk-validated** server-side, and **manually approved** by a human before an
  order is sent (unless automation is explicitly set to Auto — see below).
- Risk runs **twice**: when Claude recommends, and again at approval (state may drift).
  Intents older than `INTENT_MAX_AGE_MIN` can't be approved.
- Buy orders are limit entries with an attached stop loss (Alpaca OTO); exits are managed
  by the monitor's trailing stop and partial profit-taking.
- A **daily-loss circuit breaker** halts trading at −10% on the day, and a persistent
  **kill switch** blocks all automated orders and new buys until released.
- `client_order_id` makes order submission idempotent (no double submits on retry).

## Automation modes

Set from the dashboard (Settings → Automation):

| Mode | What happens |
|------|--------------|
| **Manual** (default) | Nothing is automated. Every intent waits for your approval. |
| **Observe** | Automation runs its decision logic and records `observe_would_execute` events, but places no orders. |
| **Auto** | Eligible high-confidence intents execute, after the risk engine. Needs `AUTO_TRADING_ENABLED=true` on the server (plus `AUTO_LIVE_TRADING_CONFIRMATION` when live). |

The **kill switch** overrides every mode, persists until released, and drops the mode to
Manual. Manual sells and broker-side stop orders still work while it is engaged.

## Models

Each layer has its own model and effort level (`backend/.env`):

| Layer | Default | Used for |
|-------|---------|----------|
| Strategist | `claude-opus-5-5`, effort `high` | Trade analysis |
| Position | strategist model, effort `medium` | Continuous-monitor and open-position decisions |
| Research | `claude-sonnet-5-5`, effort `medium` | Specialist analysts (later phases) |
| Watcher | `claude-haiku-5-5`, effort `low` | High-frequency market watcher (later phases) |

Every request is logged and written to the `llm_calls` table with tokens, estimated cost,
model, prompt version and outcome.

## Roadmap and status

The app is evolving into a continuously running research system: Alpaca and research
feeds → Haiku watcher → specialist analysts → Opus strategist → Python risk validation →
Alpaca execution → trade journal and evaluation. Execution stays under deterministic
control, and trading stays on paper until the system has been independently validated.

| PR | Deliverable | Status |
|----|-------------|--------|
| 00 | GitHub protections, Dependabot, CI and Claude Code workflow | Done (in review) |
| 01 | Startup, model routing, logging, schema validation, migrations (Phase 1) | Done (in review) |
| 02 | Snapshot store, batched data, market/news ingestion (Phase 2) | Done (in review); WebSocket streaming moves to PR 04 with the worker |
| 03 | Journal schema and decision/outcome tracking | Next |
| 04 | Haiku watcher skill, worker, jobs, budget and heartbeats | Planned |
| 05 | News, technical, fundamentals, macro and institutional analysts | Planned |
| 06 | Opus strategy orchestration and enriched research UI | Planned |
| 07 | Adaptive risk policies, broker reconciliation and controls | Planned |
| 08 | Historical replay, evaluation and paper experiment reports | Planned |
| 09 | Daily review, weekly optimization and automation dashboards | Planned |
| 10 | Deployment, observability, recovery and final paper review | Planned |

The first milestone is PRs 01–04: a safe, continuously running watcher that records
evidence and triggers research jobs, with automated execution kept off while signal
quality and cost are evaluated.

## Market data layer

- **Batched requests:** quotes, daily bars and news come from multi-symbol Alpaca requests
  (`DATA_CHUNK_SIZE` symbols each, at most `DATA_MAX_CONCURRENCY` in parallel). A failed
  batch is retried symbol by symbol, so one bad ticker can't blank the others. Daily bars
  are cached for `BARS_CACHE_TTL_SEC`.
- **Quality flags before the model:** every symbol gets `stale` (quote older than
  `MAX_QUOTE_AGE_SEC` while the market is open), `incomplete` (no price or history) and a
  `missing` list (unavailable fields are labeled, never guessed). Buy/sell intents for a
  stale or incomplete symbol are rejected.
- **Replayable snapshots:** each analysis run stores the exact context the model saw, with
  its quality flags, feed and market state, in `market_snapshots`
  (`GET /analysis/snapshots/{id}`); `analysis_runs.market_snapshot_id` links the run to it.
- **History:** daily bars (`market_bars`), latest quotes (`latest_quotes`) and news
  (`news_events`) are stored. News is deduplicated across sources and symbols, marked
  `is_new` the first time it is seen, classified by event type, and shown to the model as
  citable `news:<id>` items.
- **Market context:** index trends, sector breadth, sector leaders/laggards and realized
  volatility from index and sector ETFs (`MARKET_CONTEXT_SYMBOLS`).
- **Coverage:** the free IEX feed carries only part of US volume; the feed in use is
  recorded with every snapshot.
- **Streaming worker:** `python -m app.workers.market_stream` (from `backend/`, separate from
  the web server) subscribes to Alpaca quotes, minute bars and news over WebSockets for the
  watchlist and open positions, buffers writes into `latest_quotes`, `market_bars` (1Min) and
  `news_events`, refills missed minute bars from REST every `STREAM_REFILL_SEC`, logs a
  heartbeat, and shuts down cleanly on Ctrl-C. It only reads market data and positions; it
  never places orders. Free Alpaca plans allow one market-data WebSocket at a time.

## Research data

Fundamentals, filings and macro data are fetched by a separate refresh command, never inside a
web request, and stored with their period, filing (release) date and source:

```bash
python -m app.services.research_data.refresh --symbols AAPL,MSFT,NVDA
```

- **SEC EDGAR** (needs `SEC_USER_AGENT`): ticker → CIK, company SIC code mapped to a sector
  (replacing the old 27-symbol table), recent 8-K/10-Q/10-K filings, and XBRL facts (revenue,
  net income, diluted EPS, gross profit, operating income). Rate-limited to 5 requests/second.
- **FRED** (needs `FRED_API_KEY`): 10-year and 2-year Treasury yields, fed funds, CPI,
  unemployment and VIX, keeping each vintage for point-in-time backtests.
- The model context reads only stored values: latest fundamentals and growth with their
  dates, the sector, the last filing, and a `macro` block. Anything unavailable stays null
  and is listed as missing.

## Trade journal

Every proposal, order and outcome is linked so results can be measured, including losses
and ideas that were rejected or never traded:

- Intents record their strategy (`claude-analysis` or `continuous-monitor`), prompt version,
  the evidence the model cited, the market snapshot they came from, and an expiry.
- A read-only order sync (every monitor cycle, in every mode, and `POST /journal/sync`) pulls
  order status and fills from Alpaca into `order_fills`, opens a `trade_outcomes` row when a
  buy fills and closes it when the position's sell or protective stop fills, with realized
  P&L, holding days, exit reason and the SPY return over the same period.
- `candidate_outcomes` records what every intent (approved, rejected, watch or avoid) would
  have returned over 1, 5 and 20 trading days using one fixed rule (`fwd-close-v1`) and stored
  daily bars, so rejected ideas can be compared with executed ones.
- Read it at `GET /journal/trades` and `GET /journal/candidates`.
- Known gap: positions closed with a direct close-position call (the monitor's exits) have no
  order record yet, so their outcomes stay open until broker reconciliation lands (PR 07).

## Backtesting

`app/backtest/` replays strategies over stored daily bars (`market_bars`) with the same
indicator, prescreen and risk logic the live app uses — no Claude calls:

```bash
python -m app.backtest.run --symbols AAPL,MSFT,NVDA --start 2025-01-02 --end 2026-11-13 \
    --split holdout --out report.json          # or --split walk-forward / none
```

- **Point-in-time:** on simulated day *d* a strategy can only read bars up to *d*; reading
  ahead raises an error (tested). About 420 days of warm-up history are loaded before `--start`.
- **Fills:** signals at the close of *d*; limit orders fill on *d+1* at min(open, limit) plus
  slippage, never above the limit; stops win ties with targets and gap fills use the open;
  fills are capped at a share of bar volume; fees and slippage are configurable.
- **Strategies:** a deterministic momentum baseline (top-N by the prescreener score, sized
  by `risk.evaluate`) and buy-and-hold SPY; `ReplayStrategy` will replay cached AI signals.
- **Splits:** chronological development / validation / holdout, or walk-forward. Parameters
  are chosen on validation (or train) windows; the holdout runs once, last.
- **Report:** net and benchmark-relative return, CAGR, max drawdown, volatility, Sharpe-like
  ratio, win rate, average win/loss, expectancy, trades, turnover, exposure, a bootstrap
  95% interval for the mean trade return, and a breakdown by market regime.

## Development workflow

- `Production` is the default branch. Work happens on `agent/<issue>-<slug>` branches and
  lands through pull requests; nothing is pushed to `Production` directly.
- CI (`.github/workflows/ci.yml`) runs ruff, the pytest suite (with a throwaway Postgres
  for migration tests) and the frontend build on every PR. It uses no secrets, and
  Claude and Alpaca are mocked.
- Changes to risk, execution, broker, automation, migrations, secrets templates or CI are
  owned by `.github/CODEOWNERS` and need explicit human review.
- Dependabot opens weekly dependency PRs; the Anthropic and Alpaca SDKs, SQLAlchemy,
  psycopg and Alembic always arrive as separate PRs.
- `CLAUDE.md` holds the rules for Claude Code in this repository (safety boundaries,
  commands, definition of done); `.claude/agents/` and `.claude/skills/` define the
  development agents and workflows (plan an issue, implement it, review a PR, fix CI, run
  several issues in parallel).
- **Parallel agents and `PROGRESS.txt`:** roadmap issues run in parallel, one agent per issue
  in its own git worktree. Open `PROGRESS.txt` at the repo root to watch them: a progress bar
  per issue, its state and current step, and how many agents are working, refreshed every
  minute by `python3 tools/agent_progress.py watch`. Agents report milestones with
  `tools/agent_progress.py update`; the lead marks an issue INTEGRATED only after reviewing,
  re-testing and pushing it. The board is local and not committed.

## Project layout

```
.
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI app; refuses to start on an outdated schema
│   │   ├── db_migrate.py        # `python -m app.db_migrate` (Alembic upgrade)
│   │   ├── logging_config.py    # structured JSON logs with secret redaction
│   │   ├── config.py            # env-driven settings
│   │   ├── database.py          # SQLAlchemy engine/session
│   │   ├── models.py            # DB models (audit, intents, orders, ...)
│   │   ├── schemas.py           # Pydantic request/response + Claude contract
│   │   ├── deps.py              # auth + DB session dependencies
│   │   ├── services/
│   │   │   ├── risk.py          # the risk engine (pure, tested)
│   │   │   ├── llm.py           # the one Claude client (routing, structured output, accounting)
│   │   │   ├── analyst.py       # Claude prompts
│   │   │   ├── intent_validation.py  # fail-closed checks on model output
│   │   │   ├── automation.py    # modes, kill switch, the automated-order gate
│   │   │   ├── broker.py        # Alpaca paper/live execution
│   │   │   └── data_aggregator.py
│   │   └── routers/             # API routes
│   ├── migrations/              # Alembic revisions
│   ├── tests/
│   ├── requirements.txt         # runtime deps (requirements-dev.txt: lint)
│   └── .env.example
└── frontend/
    ├── src/                     # React dashboard
    ├── package.json
    └── .env.example
```

## Setup

### 1. Prerequisites
- Python 3.11+
- Node 18+
- PostgreSQL 14+ (running locally or remote)
- An [Alpaca paper trading](https://alpaca.markets/) key/secret
- An [Anthropic API key](https://console.anthropic.com/)

### 2. Database
```bash
createdb trading_assistant      # or use an existing Postgres instance
```

### 3. Backend
```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env             # then edit .env with your keys
python -m app.db_migrate         # create or upgrade the schema (back up first if it has data)
uvicorn app.main:app --reload --port 8000
```
The app never changes the schema itself; it refuses to start until `python -m app.db_migrate`
has brought the database to the latest revision. A database created by an older version of
this app is detected, brought to the baseline, stamped and upgraded automatically. Back it up
first: `pg_dump -Fc -d trading_assistant -f backups/before-migrate.dump`.

API docs at http://localhost:8000/docs

Lint and tests (CI runs the same):
```bash
ruff check app tests migrations
pytest -q
# migration tests need a disposable database whose name contains "test":
createdb trading_assistant_test
TEST_DATABASE_URL=postgresql+psycopg://localhost:5432/trading_assistant_test pytest -q
```

### 4. Frontend
```bash
cd frontend
npm install
cp .env.example .env             # set VITE_API_URL and VITE_API_TOKEN
npm run dev
```
Dashboard at http://localhost:5173

### 5. Going live (do NOT skip the paper phase)
Live trading stays off until you set `TRADING_MODE=live` in `backend/.env`. Keep it on
`paper` until you have soaked the full loop on the Alpaca paper account. Each live order
still requires a confirmation in the UI.

## Environment variables (backend/.env)

| Var | Description |
|-----|-------------|
| `DATABASE_URL` | `postgresql+psycopg://user:pass@localhost:5432/trading_assistant` |
| `ANTHROPIC_API_KEY` | Claude API key |
| `STRATEGIST_MODEL` / `STRATEGIST_EFFORT` | Trade-analysis model and effort (default `claude-opus-5-5` / `high`) |
| `POSITION_MODEL` / `RESEARCH_MODEL` / `WATCHER_MODEL` | Per-layer models (and matching `*_EFFORT`) |
| `MAX_DATA_AGE_SEC` / `INTENT_MAX_AGE_MIN` / `MAX_LIMIT_DEVIATION_PCT` | Fail-closed freshness and limit-price checks |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` and `json` (or `text`) |
| `ALPACA_KEY` / `ALPACA_SECRET` | Alpaca API credentials |
| `TRADING_MODE` | `paper` (default) or `live` |
| `AUTO_TRADING_ENABLED` | Server permission for Auto mode, default `false` |
| `API_TOKEN` | Shared bearer token the frontend sends (set anything; keep secret) |
| `DAILY_LOSS_LIMIT_PCT` | Kill-switch threshold, default `10` |
| `NEWS_API_KEY` | Optional — enables newsapi.org headlines |

> Secrets live only in `backend/.env`. They are never sent to the browser and never
> placed in a Claude prompt.
