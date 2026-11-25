"""lumen-embedder: centralised embedding service.

    POST /v1/embed          native: {model, inputs, input_type, dimensions, dtype}
    POST /v1/embeddings     OpenAI-compatible (any OpenAI SDK works)
    POST /embed             TEI-compatible drop-in: Atlas can point ATLAS_EMBED_URL here unchanged
    POST /rerank            TEI-compatible passthrough to the reranker
    GET  /v1/models | /health | /metrics

TEI-compatible calls carry no input type; send ``X-Input-Type: query`` to get query priority.
No prefixes are added on /embed (callers like Atlas already apply model instructions).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

import numpy as np
from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from lumen.common.errors import LumenError, Unauthorized
from lumen.common.logging import configure_logging, get_logger
from lumen.common.settings import get_settings
from lumen.common.telemetry import setup_telemetry
from lumen.embeddings.backends import ModelRegistry
from lumen.embeddings.service import EmbeddingService
from lumen.storage.clickhouse import EMBED_COLUMNS, ClickHouse, connect

log = get_logger(__name__)


async def _flush_log(app: FastAPI) -> None:
    """Request telemetry → ClickHouse every 10 s in one batch (never per request)."""
    while True:
        await asyncio.sleep(10)
        rows = app.state.svc.drain_log()
        if rows and app.state.ch is not None:
            try:
                await app.state.ch.insert_rows("embedding_requests", EMBED_COLUMNS, rows,
                                               batch_id=f"emb:{id(rows)}:{rows[0]['ts'].timestamp()}")
            except Exception:
                log.warning("embed_log_flush_failed", rows=len(rows))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    s = get_settings()
    configure_logging(s.log_level)
    setup_telemetry(f"{s.service_name}-embedder", s.otlp_endpoint)
    redis = Redis.from_url(s.redis_url)
    svc = EmbeddingService(ModelRegistry.load(s.embed_backends_file), redis, max_batch_tokens=s.embed_max_batch_tokens,
                           max_wait_ms=s.embed_max_wait_ms, cache_ttl_s=s.embed_cache_ttl_s)
    svc.start()
    try:
        ch: ClickHouse | None = ClickHouse(await connect(s.clickhouse_url))
    except Exception:
        log.warning("clickhouse_unavailable_embed_logging_disabled")
        ch = None
    app.state.svc, app.state.ch = svc, ch
    flusher = asyncio.create_task(_flush_log(app))
    yield
    flusher.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await flusher
    await svc.stop()
    if ch:
        await ch.close()
    await redis.aclose()


app = FastAPI(title="lumen-embedder", version="0.1.0", lifespan=lifespan)


@app.exception_handler(LumenError)
async def lumen_error(_: Request, exc: LumenError) -> JSONResponse:
    headers = {"Retry-After": str(int(exc.details["retry_after"]))} if "retry_after" in exc.details else None
    return JSONResponse({"type": exc.code, "title": exc.code, "status": exc.status_code, "detail": exc.message},
                        status_code=exc.status_code, media_type="application/problem+json", headers=headers)


async def auth(authorization: Annotated[str | None, Header()] = None) -> None:
    secret = get_settings().embed_api_key
    key = secret.get_secret_value() if secret else ""
    if key and authorization != f"Bearer {key}":
        raise Unauthorized("invalid embedding service key")


Auth = Annotated[None, Depends(auth)]


def svc(request: Request) -> EmbeddingService:
    return request.app.state.svc  # type: ignore[no-any-return]


class EmbedRequest(BaseModel):
    inputs: list[str] = Field(min_length=1, max_length=2048)
    model: str | None = None
    input_type: Literal["query", "document"] = "document"
    dimensions: int | None = Field(None, ge=8)
    dtype: Literal["float", "int8", "binary"] = "float"


@app.post("/v1/embed")
async def embed(body: EmbedRequest, request: Request, _: Auth) -> dict[str, Any]:
    r = await svc(request).embed(body.inputs, model=body.model, input_type=body.input_type,
                                 dimensions=body.dimensions, dtype=body.dtype)
    return {"model": r.model, "embeddings": r.vectors, "cache_hits": r.cache_hits, "tokens": r.tokens}


class OpenAIEmbeddings(BaseModel):
    input: str | list[str]
    model: str | None = None
    dimensions: int | None = None
    encoding_format: Literal["float", "base64"] = "float"
    input_type: Literal["query", "document"] = "document"  # extension (ignored by OpenAI SDKs)


@app.post("/v1/embeddings")
async def openai_embeddings(body: OpenAIEmbeddings, request: Request, _: Auth) -> dict[str, Any]:
    inputs = [body.input] if isinstance(body.input, str) else body.input
    r = await svc(request).embed(inputs, model=body.model, input_type=body.input_type, dimensions=body.dimensions)
    data = []
    for i, v in enumerate(r.vectors):
        emb: Any = v
        if body.encoding_format == "base64":
            emb = base64.b64encode(np.asarray(v, dtype=np.float32).tobytes()).decode()
        data.append({"object": "embedding", "index": i, "embedding": emb})
    return {"object": "list", "model": r.model, "data": data,
            "usage": {"prompt_tokens": r.tokens, "total_tokens": r.tokens}}


class TEIEmbed(BaseModel):
    inputs: str | list[str]
    normalize: bool = True
    truncate: bool = True


@app.post("/embed")
async def tei_embed(body: TEIEmbed, request: Request, _: Auth,
                    x_input_type: Annotated[str | None, Header()] = None) -> list[list[float]]:
    inputs = [body.inputs] if isinstance(body.inputs, str) else body.inputs
    r = await svc(request).embed(inputs, input_type="query" if x_input_type == "query" else "document")
    return r.vectors  # type: ignore[return-value]


@app.post("/rerank")
async def rerank(request: Request, _: Auth) -> Any:
    url = get_settings().rerank_url
    if not url:
        raise LumenError("no reranker configured")
    return await svc(request).rerank(url, await request.json())


@app.get("/v1/models")
async def models(request: Request) -> dict[str, Any]:
    reg = svc(request).registry
    return {"default": reg.default, "models": [
        {"id": m.id, "backend": m.backend, "upstream_model": m.upstream_model, "dim": m.dim,
         "max_tokens": m.max_tokens, "batch_mean": svc(request).batchers[m.id].stats.mean_batch}
        for m in reg.specs.values()]}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics")
async def metrics() -> PlainTextResponse:
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)
