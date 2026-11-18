"""FastAPI application entrypoint."""
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import db_migrate
from app.config import settings
from app.logging_config import configure_logging, log_event
from app.routers import analysis, automation, intents, journal, orders, portfolio
from app.routers import monitor as monitor_router
from app.routers import risk as risk_router
from app.routers import research as research_router
from app.routers import usage as usage_router
from app.routers import watcher as watcher_router
from app.routers import watchlist
from app.services import llm, monitor

configure_logging()
logger = logging.getLogger("trading.app")

app = FastAPI(title="AI Trading Assistant", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    # The app never changes the schema at startup. Refuse to start against a database that
    # is not at the latest Alembic revision; run `python -m app.db_migrate` (after a backup).
    status = db_migrate.schema_status()
    if not status["up_to_date"]:
        raise RuntimeError(
            f"Database schema is at revision {status['current']!r} but the code expects "
            f"{status['head']!r}. Back up the database, then run: python -m app.db_migrate"
        )
    # Fail fast on a bad model/effort setting instead of failing every Claude call later.
    roles = {name: llm.get_role(name) for name in ("strategist", "position", "research", "watcher")}
    log_event(logger, "app.startup", schema_revision=status["current"],
              models={n: f"{r.model}/{r.effort}" for n, r in roles.items()},
              trading_mode=settings.trading_mode, is_paper=not settings.is_live,
              auto_trading_enabled=settings.auto_trading_enabled,
              continuous_monitor_enabled=settings.continuous_monitor_enabled)
    # Background position monitor. It is a no-op while automation is inactive (default).
    monitor.start()


@app.on_event("shutdown")
async def on_shutdown():
    monitor.stop()


@app.get("/health", tags=["health"])
def health():
    return {"status": "ok", "trading_mode": settings.trading_mode}


app.include_router(portfolio.router)
app.include_router(watchlist.router)
app.include_router(analysis.router)
app.include_router(intents.router)
app.include_router(orders.router)
app.include_router(risk_router.router)
app.include_router(automation.router)
app.include_router(monitor_router.router)
app.include_router(journal.router)
app.include_router(research_router.router)
app.include_router(usage_router.router)
app.include_router(watcher_router.router)
