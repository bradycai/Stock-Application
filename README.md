# AI Trading Assistant

A full-stack trading assistant that connects Claude to an Alpaca brokerage account
through a controlled backend. **Claude never places trades.** It analyzes market data
and returns structured JSON trade *intents*. A deterministic risk engine validates every
intent, a human approves each one, and only then does the backend submit an order to
Alpaca.

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
