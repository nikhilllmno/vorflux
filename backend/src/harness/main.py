"""FastAPI application factory and local entry point."""

from dataclasses import asdict
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from harness import __version__
from harness.config import Settings, get_settings
from harness.runtimes import RuntimeRegistry


def build_registry(settings: Settings) -> RuntimeRegistry:
    """Register the production adapters. None exist yet (Claude Agent SDK lands in M1)."""
    return RuntimeRegistry()


def create_app(
    settings: Settings | None = None, runtimes: RuntimeRegistry | None = None
) -> FastAPI:
    """`runtimes` lets tests inject a registry with test doubles."""
    settings = settings or get_settings()
    app = FastAPI(title="Hotel Agent Harness", version=__version__)
    app.state.settings = settings
    app.state.runtimes = runtimes if runtimes is not None else build_registry(settings)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__, "environment": settings.environment}

    @app.get("/v1/runtimes")
    def list_runtimes(request: Request) -> dict[str, list[dict[str, Any]]]:
        registry: RuntimeRegistry = request.app.state.runtimes
        return {
            "runtimes": [
                {"kind": kind, "capabilities": asdict(registry.get(kind).capabilities())}
                for kind in registry.kinds()
            ]
        }

    return app


def run() -> None:
    import uvicorn

    uvicorn.run("harness.main:create_app", factory=True, host="127.0.0.1", port=8000)
