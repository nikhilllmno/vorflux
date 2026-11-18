# Control-plane API image (FastAPI + uv). Build context: backend/.
#
#   docker build -t harness-api:local backend
#   docker run --rm -p 8080:8080 -e HARNESS_ENVIRONMENT=local harness-api:local
#
# Deployed only by a human with owner approval (docs/runbook-deploy.md). CI builds this image
# to validate it and never pushes it. No secret is baked in: Fly.io injects secrets at runtime.

ARG PYTHON_IMAGE=python:3.12-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85

FROM ${UV_IMAGE} AS uv

# --- Build stage: resolve locked runtime dependencies into /app/.venv ---
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
# Dependencies only (no dev group). The project itself is not installed: its metadata points
# to ../README.md, which is outside this build context. Source goes on PYTHONPATH instead.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# --- Runtime stage: no uv, no build tools, non-root user ---
FROM ${PYTHON_IMAGE} AS runtime
RUN groupadd --system --gid 10001 harness \
    && useradd --system --uid 10001 --gid harness --home-dir /app --shell /usr/sbin/nologin harness
WORKDIR /app
COPY --from=build --chown=harness:harness /app/.venv /app/.venv
COPY --chown=harness:harness src/ /app/src/
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT', '8080')}/healthz\", timeout=2)"]
CMD ["sh", "-c", "exec uvicorn harness.main:create_app --factory --host 0.0.0.0 --port \"${PORT}\" --no-server-header"]
