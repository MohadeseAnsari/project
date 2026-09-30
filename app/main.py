"""FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api.limits import limiter
from app.api.routes import router
from app.config import settings
from app.db.connection import engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Configure logging on startup and dispose the engine on shutdown."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    yield
    await engine.dispose()


app = FastAPI(
    title="IranDoc News Generation",
    description="Automatic journalistic news generation from IranDoc thesis metadata.",
    version="1.0.0",
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.include_router(router)


@app.get("/")
async def root():
    """Service identity endpoint."""
    return {"service": "IranDoc News Generation", "version": "1.0.0"}
