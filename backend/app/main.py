"""Claude HQ Arena -- the multiplayer backend."""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .db import Base, describe_backend, engine
from .routes import auth as auth_routes
from .routes import board as board_routes
from .routes import nudges as nudge_routes
from .routes import raids as raid_routes
from .routes import rooms as room_routes
from .routes import stats as stats_routes


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Alembic owns the schema in production; this only helps SQLite dev/test runs.
    if get_settings().is_sqlite:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


app = FastAPI(title="Claude HQ Arena", version="0.1.0", lifespan=lifespan)

# The dashboard proxies API calls server-side, so the browser never calls us
# cross-origin for authenticated routes. Websockets are exempt from CORS and are
# guarded by short-lived tickets instead.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8765", "http://localhost:8765"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(auth_routes.router)
app.include_router(stats_routes.router)
app.include_router(board_routes.router)
app.include_router(nudge_routes.router)
app.include_router(raid_routes.router)
app.include_router(room_routes.router)


@app.get("/health")
async def health() -> dict:
    try:
        db = await describe_backend()
    except Exception as exc:
        # Report unhealthy rather than 200-with-a-broken-database.
        return {"ok": False, "service": "claude-hq-arena", "db": f"unreachable: {exc}"}
    return {"ok": True, "service": "claude-hq-arena", "db": db}
