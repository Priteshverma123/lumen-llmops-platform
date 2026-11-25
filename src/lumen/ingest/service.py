"""OTLP receive path: S3-first durability, then a reference on a Redis stream.

    client ──OTLP/HTTP──▶ api: validate size/auth ─▶ PUT raw batch to MinIO ─▶ XADD lumen:ingest
                                 (200 only after both succeed)

Why S3 first (Langfuse v3/v4 design): the API acknowledges a batch only once it is durable in
object storage, so a ClickHouse outage or a worker crash never loses telemetry: the worker
re-reads the raw batch. Redis carries only a small pointer, never the payload.

Backpressure: when the stream backlog exceeds ``MAX_BACKLOG`` the API answers 429 with
Retry-After, which OTLP exporters treat as retryable. Overload therefore degrades into exporter
retries instead of unbounded memory growth.
"""

from __future__ import annotations

import gzip
import uuid
from datetime import UTC, datetime

from redis.asyncio import Redis

from lumen.common.errors import InvalidInput, Overloaded, PayloadTooLarge
from lumen.storage.objects import ObjectStore

STREAM = "lumen:ingest"
DLQ = "lumen:ingest:dlq"
GROUP = "ingest-workers"
MAX_BACKLOG = 50_000


async def accept_batch(redis: Redis, objects: ObjectStore, *, project_id: str, body: bytes, content_type: str,
                       content_encoding: str | None, max_bytes: int) -> str:
    if content_encoding == "gzip":
        try:
            body = gzip.decompress(body)
        except OSError as exc:
            raise InvalidInput("invalid gzip body") from exc
    if len(body) > max_bytes:
        raise PayloadTooLarge(f"batch exceeds {max_bytes // (1024 * 1024)} MB")
    if not body:
        raise InvalidInput("empty body")
    fmt = "pb" if "protobuf" in content_type else "json" if "json" in content_type else None
    if fmt is None:
        raise InvalidInput("content-type must be application/x-protobuf or application/json")
    if await redis.xlen(STREAM) > MAX_BACKLOG:
        raise Overloaded("ingest backlog is full, retry later", retry_after=5)
    batch_id = uuid.uuid4().hex
    now = datetime.now(UTC)
    key = f"otlp/{project_id}/{now:%Y/%m/%d/%H}/{batch_id}.{fmt}"
    await objects.put(key, body, "application/x-protobuf" if fmt == "pb" else "application/json")
    await redis.xadd(STREAM, {"batch_id": batch_id, "key": key, "project_id": project_id, "format": fmt})
    return batch_id
