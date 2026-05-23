"""
Application entrypoint.

Lifespan pattern (FastAPI 0.95+):
  - Replaces @app.on_event("startup") / ("shutdown")
  - Resources created before yield are available for the app's lifetime
  - Resources after yield are cleaned up on shutdown
  - This pattern works correctly with async context managers
"""
from contextlib import asynccontextmanager
from typing import AsyncGenerator
import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import time

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.database import engine, Base

# Import all models so SQLAlchemy knows about them for create_all
from app.db.models import payment, webhook_event  # noqa: F401
from app.api.routes import payments

settings = get_settings()
configure_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup and shutdown logic."""
    logger.info("app_starting", environment=settings.environment)

    # Create tables (in production, use Alembic migrations instead)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    logger.info("database_tables_ready")

    yield  # ← app runs here

    # Cleanup
    await engine.dispose()
    logger.info("app_shutdown")


app = FastAPI(
    title="Payment Intelligence Platform",
    description="AI-powered payment processing with fraud detection",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if not settings.is_production else None,  # hide Swagger in prod
    redoc_url="/redoc" if not settings.is_production else None,
)

# ── Middleware ────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if not settings.is_production else ["https://yourdomain.com"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    """Log every request with latency — essential for payments SLA tracking."""
    start = time.perf_counter()

    # Bind request context for all log lines within this request
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        method=request.method,
        path=request.url.path,
    )

    response = await call_next(request)
    latency_ms = round((time.perf_counter() - start) * 1000, 2)

    logger.info(
        "http_request",
        status_code=response.status_code,
        latency_ms=latency_ms,
    )

    response.headers["X-Response-Time-Ms"] = str(latency_ms)
    return response


# ── Routers ───────────────────────────────────────────────

app.include_router(payments.router, prefix="/api/v1")


# ── Health check ─────────────────────────────────────────

@app.get("/health", tags=["infrastructure"])
async def health_check() -> dict:
    """
    Used by Docker healthchecks and load balancers.
    Should return quickly — never do DB queries here.
    """
    return {
        "status": "healthy",
        "environment": settings.environment,
        "version": "0.1.0",
    }
