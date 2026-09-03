"""Recoup — FastAPI application entrypoint."""

from __future__ import annotations

import sentry_sdk
import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.webhook.router import router as webhook_router

if settings.sentry_dsn:
    sentry_sdk.init(dsn=settings.sentry_dsn, traces_sample_rate=0.1)

log = structlog.get_logger(__name__)


def create_app() -> FastAPI:
    """Factory that builds and configures the FastAPI application."""
    app = FastAPI(
        title="Recoup",
        description="Autonomous Revenue Recovery & Smart Fallback Orchestrator",
        version="0.1.0",
        docs_url="/docs" if settings.app_env != "production" else None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["POST", "GET"],
        allow_headers=["*"],
    )

    app.include_router(webhook_router, prefix="/webhooks")

    from app.ops.router import ops_router
    from app.recovery.router import recovery_router

    app.include_router(ops_router)

    app.include_router(recovery_router)

    @app.get("/health", tags=["infra"])
    async def health_check() -> dict[str, str]:
        return {"status": "ok", "env": settings.app_env}

    app.mount("/", StaticFiles(directory="app/templates", html=True), name="static")

    @app.on_event("startup")
    async def on_startup() -> None:
        log.info("Recoup starting up", env=settings.app_env)

    @app.on_event("shutdown")
    async def on_shutdown() -> None:
        log.info("Recoup shutting down")

    return app


app = create_app()
