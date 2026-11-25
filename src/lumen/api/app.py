"""lumen-api: OTLP ingest + query API + prompt registry + experiments (OpenAPI at /docs)."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import structlog
from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from redis.asyncio import Redis
from sqlalchemy import text

from lumen.api.routers import quality, registry, telemetry
from lumen.common.errors import LumenError
from lumen.common.logging import configure_logging, get_logger
from lumen.common.settings import get_settings
from lumen.common.telemetry import setup_telemetry
from lumen.registry.service import Registry
from lumen.storage.clickhouse import ClickHouse, connect
from lumen.storage.objects import ObjectStore
from lumen.storage.postgres import engine

log = get_logger(__name__)
REQS = Counter("lumen_http_requests_total", "HTTP requests", ["method", "route", "status"])
LAT = Histogram("lumen_http_latency_seconds", "HTTP latency", ["route"],
                buckets=(0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    s = get_settings()
    configure_logging(s.log_level)
    setup_telemetry(f"{s.service_name}-api", s.otlp_endpoint)
    app.state.settings = s
    app.state.redis = Redis.from_url(s.redis_url)
    app.state.objects = ObjectStore(s)
    await app.state.objects.ensure_bucket()
    app.state.ch = ClickHouse(await connect(s.clickhouse_url))
    app.state.registry = Registry(app.state.redis)
    app.state.arq = await create_pool(RedisSettings.from_dsn(s.redis_url))
    log.info("lumen_api_ready")
    yield
    await app.state.arq.aclose()
    await app.state.ch.close()
    await app.state.redis.aclose()


app = FastAPI(title="Lumen", version="0.1.0", lifespan=lifespan,
              description="LLMOps control plane: OTel-native observability, prompt registry, experiments.")


@app.middleware("http")
async def ctx(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex
    structlog.contextvars.bind_contextvars(request_id=rid)
    t0 = time.perf_counter()
    try:
        resp = await call_next(request)
    finally:
        structlog.contextvars.clear_contextvars()
    path = getattr(request.scope.get("route"), "path", "unmatched")
    REQS.labels(request.method, path, resp.status_code).inc()
    LAT.labels(path).observe(time.perf_counter() - t0)
    resp.headers["x-request-id"] = rid
    return resp


def problem(status: int, code: str, detail: str, extra: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"type": f"https://lumen.dev/errors/{code}", "title": code, "status": status,
                         "detail": detail, **(extra or {})}, status_code=status,
                        media_type="application/problem+json", headers=headers)


@app.exception_handler(LumenError)
async def lumen_error(_: Request, exc: LumenError) -> JSONResponse:
    headers = {"Retry-After": str(max(1, round(float(exc.details["retry_after"]))))} \
        if "retry_after" in exc.details else None  # type: ignore[arg-type]
    return problem(exc.status_code, exc.code, exc.message, {"details": exc.details} if exc.details else None, headers)


@app.exception_handler(RequestValidationError)
async def validation(_: Request, exc: RequestValidationError) -> JSONResponse:
    return problem(422, "invalid_input", "request validation failed", {"errors": exc.errors()[:20]})


@app.exception_handler(Exception)
async def unhandled(_: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled_error")
    return problem(500, "internal_error", "internal server error")


@app.get("/health/live", tags=["health"])
async def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"])
async def ready(request: Request) -> JSONResponse:
    checks: dict[str, str] = {}

    async def pg() -> None:
        async with engine().connect() as c:
            await c.execute(text("SELECT 1"))

    for name, probe in (("postgres", pg), ("redis", request.app.state.redis.ping),
                        ("clickhouse", request.app.state.ch.ping)):
        try:
            await probe()
            checks[name] = "ok"
        except Exception as e:
            checks[name] = f"error: {type(e).__name__}"
    backlog = await request.app.state.redis.xlen("lumen:ingest")
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse({"status": "ok" if ok else "degraded", "checks": checks, "ingest_backlog": backlog},
                        status_code=200 if ok else 503)


@app.get("/metrics", include_in_schema=False)
async def metrics() -> PlainTextResponse:
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


for r in (telemetry.router, registry.router, quality.router):
    app.include_router(r)
