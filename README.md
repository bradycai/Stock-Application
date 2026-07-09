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
- Every intent is **schema-validated** (structured output), then **risk-validated**
  server-side, then **manually approved** by a human before an order is sent.
- Risk runs **twice**: when Claude recommends, and again at approval (state may drift).
- Orders are **bracket orders** — the stop loss is attached atomically with the entry.
- A **daily-loss kill switch** halts all trading at −10% on the day.
- `client_order_id` makes order submission idempotent (no double submits on retry).

## Project layout

```
.
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI app + table creation
│   │   ├── config.py            # env-driven settings
│   │   ├── database.py          # SQLAlchemy engine/session
│   │   ├── models.py            # DB models (audit, intents, orders, ...)
│   │   ├── schemas.py           # Pydantic request/response + Claude contract
│   │   ├── deps.py              # auth + DB session dependencies
│   │   ├── services/
│   │   │   ├── risk.py          # the risk engine (pure, tested)
│   │   │   ├── analyst.py       # Claude integration (structured output)
│   │   │   ├── broker.py        # Alpaca paper/live execution
│   │   │   └── data_aggregator.py
│   │   └── routers/             # API routes
│   ├── tests/test_risk.py
│   ├── requirements.txt
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
pip install -r requirements.txt
cp .env.example .env             # then edit .env with your keys
uvicorn app.main:app --reload --port 8000
```
Tables are created automatically on startup. API docs at http://localhost:8000/docs

Run the risk-engine tests:
```bash
pytest -q
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
| `ALPACA_KEY` / `ALPACA_SECRET` | Alpaca API credentials |
| `TRADING_MODE` | `paper` (default) or `live` |
| `API_TOKEN` | Shared bearer token the frontend sends (set anything; keep secret) |
| `DAILY_LOSS_LIMIT_PCT` | Kill-switch threshold, default `10` |
| `NEWS_API_KEY` | Optional — enables newsapi.org headlines |

> Secrets live only in `backend/.env`. They are never sent to the browser and never
> placed in a Claude prompt.
