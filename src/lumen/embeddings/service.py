"""Embedding service core: cache → single-flight → micro-batcher → backend → post-processing.

Cache key = ``emb:{model_id}:{input_type}:{sha256(normalised text)}``. The *versioned* model id is
in the key, so vectors from two model versions can never be mixed: the classic silent bug when a
team upgrades an embedding model behind a stable name.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import numpy as np
from prometheus_client import Counter
from redis.asyncio import Redis

from lumen.common.errors import InvalidInput
from lumen.embeddings.backends import ModelRegistry, ModelSpec, OpenAIBackend, TEIBackend
from lumen.embeddings.batcher import MicroBatcher, est_tokens

CACHE = Counter("lumen_embed_cache_total", "Embedding cache lookups", ["model", "result"])
TOKENS = Counter("lumen_embed_tokens_total", "Estimated tokens sent to backends", ["model"])


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


def cache_key(model_id: str, input_type: str, text: str) -> str:
    return f"emb:{model_id}:{input_type}:{hashlib.sha256(normalize(text).encode()).hexdigest()}"


def postprocess(vec: list[float], dimensions: int | None, dtype: str) -> list[float] | list[int]:
    """Matryoshka truncation (+ re-normalisation) and optional quantisation for storage savings."""
    v = np.asarray(vec, dtype=np.float32)
    if dimensions:
        if dimensions > v.shape[0]:
            raise InvalidInput(f"dimensions {dimensions} > model dim {v.shape[0]}")
        v = v[:dimensions]
        n = np.linalg.norm(v)
        v = v / n if n else v
    if dtype == "int8":
        return np.clip(np.round(v * 127), -127, 127).astype(np.int8).tolist()
    if dtype == "binary":
        return np.packbits(v > 0).astype(np.uint8).tolist()
    return v.tolist()


@dataclass(slots=True)
class EmbedResult:
    vectors: list[Any]
    model: str
    cache_hits: int
    tokens: int


class EmbeddingService:
    def __init__(self, registry: ModelRegistry, redis: Redis | None, *, max_batch_tokens: int, max_wait_ms: float,
                 cache_ttl_s: int) -> None:
        self.registry, self.redis, self.ttl = registry, redis, cache_ttl_s
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0))
        self.batchers: dict[str, MicroBatcher] = {}
        for spec in registry.specs.values():
            backend = TEIBackend(spec, self._http) if spec.backend == "tei" else OpenAIBackend(spec, self._http)
            self.batchers[spec.id] = MicroBatcher(spec.id, backend, max_batch_tokens=max_batch_tokens,
                                                  max_wait_ms=max_wait_ms)
        self._inflight: dict[str, asyncio.Future[list[float]]] = {}
        self.request_log: list[dict[str, Any]] = []  # flushed to ClickHouse embedding_requests

    def start(self) -> None:
        for b in self.batchers.values():
            b.start()

    async def stop(self) -> None:
        for b in self.batchers.values():
            await b.stop()
        await self._http.aclose()

    def _prefixed(self, spec: ModelSpec, text: str, input_type: str) -> str:
        return (spec.query_prefix if input_type == "query" else spec.document_prefix) + text

    async def embed(self, texts: list[str], *, model: str | None = None, input_type: str = "document",
                    dimensions: int | None = None, dtype: str = "float") -> EmbedResult:
        if not texts:
            return EmbedResult([], self.registry.resolve(model).id, 0, 0)
        if len(texts) > 2048:
            raise InvalidInput("at most 2048 inputs per request")
        spec = self.registry.resolve(model)
        t0 = time.perf_counter()
        keys = [cache_key(spec.id, input_type, t) for t in texts]
        out: list[list[float] | None] = [None] * len(texts)
        if self.redis is not None:
            for i, raw in enumerate(await self.redis.mget(keys)):
                if raw is not None:
                    out[i] = np.frombuffer(raw, dtype=np.float16).astype(np.float32).tolist()
        hits = sum(v is not None for v in out)
        CACHE.labels(spec.id, "hit").inc(hits)
        CACHE.labels(spec.id, "miss").inc(len(texts) - hits)

        # single-flight: identical texts (in this request or concurrent ones) are computed once
        owned: dict[str, list[int]] = {}
        waiting: list[tuple[int, asyncio.Future[list[float]]]] = []
        loop = asyncio.get_running_loop()
        for i, k in enumerate(keys):
            if out[i] is not None:
                continue
            if k in owned:
                owned[k].append(i)
            elif k in self._inflight:
                waiting.append((i, self._inflight[k]))
            else:
                owned[k] = [i]
                self._inflight[k] = loop.create_future()
        tokens = 0
        try:
            if owned:
                own_keys = list(owned)
                inputs = [self._prefixed(spec, normalize(texts[owned[k][0]]), input_type) for k in own_keys]
                tokens = sum(est_tokens(t) for t in inputs)
                TOKENS.labels(spec.id).inc(tokens)
                vecs = await self.batchers[spec.id].submit(inputs, priority=input_type)
                pipe = self.redis.pipeline() if self.redis is not None else None
                for k, v in zip(own_keys, vecs, strict=True):
                    for i in owned[k]:
                        out[i] = v
                    self._inflight.pop(k).set_result(v)
                    if pipe is not None:
                        pipe.set(k, np.asarray(v, dtype=np.float16).tobytes(), ex=self.ttl)
                if pipe is not None:
                    await pipe.execute()
            for i, fut in waiting:
                out[i] = await fut
        except BaseException as exc:
            for k in owned:
                if (f := self._inflight.pop(k, None)) is not None and not f.done():
                    f.set_exception(exc if isinstance(exc, Exception) else RuntimeError("cancelled"))
            raise
        self.request_log.append({"ts": datetime.now(UTC), "model": spec.id, "input_type": input_type,
                                 "backend": spec.backend, "items": len(texts), "tokens": tokens, "cache_hits": hits,
                                 "batch_size": len(owned), "queue_wait_ms": 0.0,
                                 "compute_ms": (time.perf_counter() - t0) * 1000, "status": "ok"})
        vectors = [postprocess(v, dimensions, dtype) for v in out if v is not None]
        return EmbedResult(vectors, spec.id, hits, tokens)

    async def rerank(self, url: str, payload: dict[str, Any]) -> Any:
        r = await self._http.post(f"{url.rstrip('/')}/rerank", json=payload)
        r.raise_for_status()
        return r.json()

    def drain_log(self) -> list[dict[str, Any]]:
        rows, self.request_log = self.request_log, []
        return rows
