"""Ingest worker: Redis stream consumer group → decode → normalise → batched ClickHouse insert.

* At-least-once: messages are XACKed only after the insert succeeds; crashed workers' pending
  messages are reclaimed with XAUTOCLAIM after ``CLAIM_IDLE_MS``.
* Exactly-once *effect*: inserts carry a dedup token derived from the batch id, so a re-delivered
  batch does not create duplicate rows.
* Poison messages go to a dead-letter stream after ``MAX_DELIVERIES``, with the raw batch still
  in S3 for replay (re-XADD the DLQ entry to ``lumen:ingest`` after fixing the cause).
* Several stream messages are merged into one insert (ClickHouse prefers few large inserts).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import socket
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from lumen.common.logging import configure_logging, get_logger
from lumen.common.settings import Settings, get_settings
from lumen.common.telemetry import setup_telemetry
from lumen.ingest.service import DLQ, GROUP, STREAM
from lumen.pricing.catalog import PriceCatalog
from lumen.storage.clickhouse import SCORE_COLUMNS, SPAN_COLUMNS, ClickHouse, connect
from lumen.storage.objects import ObjectStore
from lumen.telemetry.otlp import decode_json, decode_protobuf, normalize

log = get_logger(__name__)
CLAIM_IDLE_MS = 60_000
MAX_DELIVERIES = 5
READ_COUNT = 50
EVAL_QUEUE = "lumen:evals"


def sampled(trace_id: str, rate: float) -> bool:
    """Deterministic per-trace sampling: every span of a trace gets the same decision, and a
    re-delivered batch samples identically (no double evaluation)."""
    if rate >= 1:
        return True
    h = int(hashlib.sha256(trace_id.encode()).hexdigest()[:8], 16)
    return h / 0xFFFFFFFF < rate


class IngestConsumer:
    def __init__(self, s: Settings, redis: Redis, objects: ObjectStore, ch: ClickHouse, catalog: PriceCatalog) -> None:
        self.s, self.r, self.objects, self.ch, self.catalog = s, redis, objects, ch, catalog
        self.name = f"{socket.gethostname()}-{os.getpid()}"

    async def ensure_group(self) -> None:
        with contextlib.suppress(ResponseError):  # BUSYGROUP: already exists
            await self.r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)

    async def _rows_for(self, fields: dict[bytes, bytes]) -> list[dict[str, Any]]:
        key = fields[b"key"].decode()
        raw = await self.objects.get(key)
        if raw is None:
            raise FileNotFoundError(key)
        spans = decode_protobuf(raw) if fields[b"format"] == b"pb" else decode_json(json.loads(raw))
        rows = normalize(spans, fields[b"project_id"].decode(), self.catalog, capture_content=self.s.capture_content,
                         content_max=self.s.content_max_chars)
        for r in rows:
            r["ingest_batch"] = fields[b"batch_id"].decode()
        return rows

    async def process(self, messages: list[tuple[bytes, dict[bytes, bytes]]]) -> None:
        good: list[bytes] = []
        rows: list[dict[str, Any]] = []
        for msg_id, fields in messages:
            try:
                rows.extend(await self._rows_for(fields))
                good.append(msg_id)
            except Exception:
                log.exception("batch_decode_failed", msg_id=msg_id.decode())
                await self._maybe_dead_letter(msg_id, fields)
        if not rows:
            if good:
                await self.r.xack(STREAM, GROUP, *good)
            return
        batch_token = hashlib.sha256(b",".join(sorted(good))).hexdigest()[:24]
        await self.ch.insert_rows("spans", SPAN_COLUMNS, rows, batch_id=batch_token, chunk=self.s.ingest_batch_rows)
        scores = [{"project_id": r["project_id"], "score_id": f"{r['span_id']}:{e['name']}", "trace_id": r["trace_id"],
                   "span_id": r["span_id"], "session_id": r["session_id"], "name": e["name"],
                   "data_type": "numeric" if e["value"] is not None else "categorical",
                   "value": float(e["value"]) if e["value"] is not None else 0.0, "string_value": e["label"],
                   "source": "eval", "comment": e["comment"], "author": r["service"], "config_id": "",
                   "created_at": r["end_time"]} for r in rows for e in r["eval_events"]]
        if scores:
            await self.ch.insert_rows("scores", SCORE_COLUMNS, scores, batch_id=batch_token)
        await self._enqueue_evals(rows)
        await self.r.xack(STREAM, GROUP, *good)
        log.info("batches_ingested", messages=len(good), spans=len(rows), scores=len(scores))

    async def _enqueue_evals(self, rows: list[dict[str, Any]]) -> None:
        picked = [r for r in rows if r["is_llm"] and sampled(r["trace_id"], self.s.eval_sample_rate)]
        if not picked:
            return
        pipe = self.r.pipeline()
        for r in picked:
            pipe.rpush(EVAL_QUEUE, json.dumps({k: r[k] for k in (
                "project_id", "trace_id", "span_id", "session_id", "input_preview", "output_preview", "finish_reason",
                "route", "prompt_name", "prompt_version")} | {"end_time": r["end_time"].isoformat()}))
        pipe.ltrim(EVAL_QUEUE, -100_000, -1)  # bounded: evals are best-effort, never block ingest
        await pipe.execute()

    async def _maybe_dead_letter(self, msg_id: bytes, fields: dict[bytes, bytes]) -> None:
        info = await self.r.xpending_range(STREAM, GROUP, min=msg_id, max=msg_id, count=1)
        deliveries = info[0]["times_delivered"] if info else 1
        if deliveries >= MAX_DELIVERIES:
            await self.r.xadd(DLQ, {**{k.decode(): v for k, v in fields.items()}, "orig_id": msg_id})
            await self.r.xack(STREAM, GROUP, msg_id)
            log.error("batch_dead_lettered", msg_id=msg_id.decode())

    async def run(self, stop: asyncio.Event) -> None:
        await self.ensure_group()
        while not stop.is_set():
            try:
                # reclaim messages from crashed consumers first
                _, claimed, _ = await self.r.xautoclaim(STREAM, GROUP, self.name, CLAIM_IDLE_MS, "0-0",
                                                        count=READ_COUNT)
                if claimed:
                    await self.process(claimed)
                resp = await self.r.xreadgroup(GROUP, self.name, {STREAM: ">"}, count=READ_COUNT, block=2000)
                for _, messages in resp or []:
                    await self.process(messages)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ingest_loop_error")  # messages stay pending → retried
                await asyncio.sleep(2)


async def main() -> None:
    s = get_settings()
    configure_logging(s.log_level)
    setup_telemetry(f"{s.service_name}-ingest", s.otlp_endpoint)
    redis = Redis.from_url(s.redis_url)
    ch = ClickHouse(await connect(s.clickhouse_url))
    consumer = IngestConsumer(s, redis, ObjectStore(s), ch, PriceCatalog.load_dir(s.pricing_dir))
    stop = asyncio.Event()
    task = asyncio.create_task(consumer.run(stop))
    try:
        await task
    finally:
        stop.set()
        await ch.close()
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
