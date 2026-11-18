"""FastAPI application entrypoint."""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app import db_migrate
from app.routers import analysis, automation, intents, orders, portfolio
from app.routers import monitor as monitor_router
from app.routers import risk as risk_router
from app.routers import watchlist
from app.services import monitor

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
