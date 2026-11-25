# syntax=docker/dockerfile:1.7
# One image, several roles (api | ingest | worker | embedder | migrate): role = command.
# Multi-stage: build deps with uv in a builder, ship only the venv + code in a slim runtime.

ARG PYTHON_VERSION=3.12

FROM ghcr.io/astral-sh/uv:0.9.30 AS uv

FROM python:${PYTHON_VERSION}-slim AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock* README.md ./
# deps layer first (cached until pyproject/lock change). --frozen when a lock file is committed.
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -f uv.lock ]; then uv sync --frozen --no-dev --no-install-project; \
    else uv sync --no-dev --no-install-project; fi
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -f uv.lock ]; then uv sync --frozen --no-dev; else uv sync --no-dev; fi

FROM python:${PYTHON_VERSION}-slim AS runtime
RUN groupadd --system --gid 10001 lumen && useradd --system --uid 10001 --gid lumen --home /app lumen \
    && apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=builder --chown=lumen:lumen /app/.venv /app/.venv
COPY --chown=lumen:lumen src ./src
COPY --chown=lumen:lumen config ./config
COPY --chown=lumen:lumen pricing ./pricing
COPY --chown=lumen:lumen clickhouse ./clickhouse
COPY --chown=lumen:lumen migrations ./migrations
COPY --chown=lumen:lumen alembic.ini ./
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app/src PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER lumen
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=5 \
    CMD curl -fsS http://localhost:8000/health/live || exit 1
CMD ["uvicorn", "lumen.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-server-header"]
