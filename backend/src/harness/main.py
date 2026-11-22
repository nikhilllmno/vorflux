"""FastAPI application factory and local entry point."""

from dataclasses import asdict
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

import harness.auth.api
from harness import __version__
from harness.config import Settings, get_settings
from harness.runtimes import FakeRuntime, RuntimeRegistry


def build_registry(settings: Settings) -> RuntimeRegistry:
    registry = RuntimeRegistry()
    if settings.environment in ("local", "test"):
        registry.register(FakeRuntime())
    return registry


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="Hotel Agent Harness", version=__version__)
    app.state.settings = settings
    app.state.runtimes = build_registry(settings)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )
    # Tenant routes authenticate with `harness.auth.require_tenant` (docs/auth.md).
    app.include_router(harness.auth.api.router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__, "environment": settings.environment}

    @app.get("/v1/runtimes")
    def list_runtimes(request: Request) -> dict[str, list[dict[str, Any]]]:
        registry: RuntimeRegistry = request.app.state.runtimes
        return {
            "runtimes": [
                {"kind": kind.value, "capabilities": asdict(registry.get(kind).capabilities())}
                for kind in registry.kinds()
            ]
        }

    return app


def run() -> None:
    import uvicorn

    uvicorn.run("harness.main:create_app", factory=True, host="127.0.0.1", port=8000)
