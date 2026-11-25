"""Dynamic micro-batcher: many small concurrent requests → few large backend calls.

TEI batches internally, but strictly FIFO with *no wait window* (core/src/queue.rs): whatever is
queued when the GPU frees up becomes the batch. Under bursty, many-small-request traffic that
yields tiny batches. Adding a few milliseconds of wait here trades a little latency for much
higher throughput (sweep: bench/batching_sweep.py).

* budget is counted like TEI's padded backends: ``longest_sequence × batch_size`` tokens;
* priority classes: interactive ``query`` items always go before bulk ``document`` items, so
  a re-indexing job cannot starve user-facing search;
* bounded queue → ``Overloaded`` (HTTP 429) instead of unbounded memory and latency.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from prometheus_client import Histogram

from lumen.common.errors import Overloaded

Backend = Callable[[list[str]], Awaitable[list[list[float]]]]

BATCH_SIZE = Histogram("lumen_embed_batch_size", "Items per backend call", ["model"],
                       buckets=(1, 2, 4, 8, 16, 32, 64, 128, 256))
QUEUE_WAIT = Histogram("lumen_embed_queue_wait_seconds", "Time an item waited for its batch", ["model", "priority"],
                       buckets=(0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.25, 1))
COMPUTE = Histogram("lumen_embed_compute_seconds", "Backend call duration", ["model"],
                    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5))


def est_tokens(text: str) -> int:
    return max(1, len(text) // 4)


@dataclass(slots=True)
class _Item:
    text: str
    tokens: int
    priority: str
    enqueued: float
    future: asyncio.Future[list[float]] = field(repr=False)


@dataclass(slots=True)
class BatchStats:
    calls: int = 0
    items: int = 0

    @property
    def mean_batch(self) -> float:
        return self.items / self.calls if self.calls else 0.0


class MicroBatcher:
    def __init__(self, name: str, backend: Backend, *, max_batch_tokens: int = 16_384, max_batch_items: int = 128,
                 max_wait_ms: float = 5.0, max_queue: int = 10_000, max_concurrency: int = 2) -> None:
        self.name, self.backend = name, backend
        self.max_tokens, self.max_items = max_batch_tokens, max_batch_items
        self.max_wait = max_wait_ms / 1000
        self.max_queue = max_queue
        self.queues: dict[str, deque[_Item]] = {"query": deque(), "document": deque()}
        self._wake = asyncio.Event()
        self._sem = asyncio.Semaphore(max_concurrency)
        self._task: asyncio.Task[None] | None = None
        self._inflight: set[asyncio.Task[None]] = set()
        self.stats = BatchStats()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name=f"batcher-{self.name}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await asyncio.gather(*self._inflight, return_exceptions=True)

    def pending(self) -> int:
        return sum(len(q) for q in self.queues.values())

    async def submit(self, texts: list[str], priority: str = "document") -> list[list[float]]:
        if self.pending() + len(texts) > self.max_queue:
            raise Overloaded(f"embedding queue full for {self.name}", retry_after=1.0)
        loop = asyncio.get_running_loop()
        now = time.perf_counter()
        prio = "query" if priority == "query" else "document"
        items = [_Item(t, est_tokens(t), prio, now, loop.create_future()) for t in texts]
        self.queues[prio].extend(items)
        self._wake.set()
        return list(await asyncio.gather(*(i.future for i in items)))

    def _take(self) -> list[_Item]:
        batch: list[_Item] = []
        longest = 0
        for prio in ("query", "document"):
            q = self.queues[prio]
            while q and len(batch) < self.max_items:
                nxt = q[0]
                padded = max(longest, nxt.tokens) * (len(batch) + 1)
                if batch and padded > self.max_tokens:
                    return batch
                batch.append(q.popleft())
                longest = max(longest, nxt.tokens)
        return batch

    def _full(self) -> bool:
        n = self.pending()
        if n >= self.max_items:
            return True
        longest = max((i.tokens for q in self.queues.values() for i in q), default=0)
        return longest * n >= self.max_tokens

    async def _loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            if not self.pending():
                continue
            # wait window starts at the oldest item's arrival; queries shorten it to keep search snappy
            oldest = min((q[0].enqueued for q in self.queues.values() if q), default=time.perf_counter())
            window = self.max_wait / 2 if self.queues["query"] else self.max_wait
            while not self._full():
                remaining = oldest + window - time.perf_counter()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=remaining)
                    self._wake.clear()
                except TimeoutError:
                    break
            while self.pending():
                batch = self._take()
                if not batch:
                    break
                await self._sem.acquire()
                t = asyncio.create_task(self._run(batch))
                self._inflight.add(t)
                t.add_done_callback(self._inflight.discard)
                if not self._full():
                    break  # leftover items get their own wait window
            if self.pending():
                self._wake.set()

    async def _run(self, batch: list[_Item]) -> None:
        try:
            started = time.perf_counter()
            for it in batch:
                QUEUE_WAIT.labels(self.name, it.priority).observe(started - it.enqueued)
            BATCH_SIZE.labels(self.name).observe(len(batch))
            try:
                vectors = await self.backend([i.text for i in batch])
            except Exception as exc:
                for it in batch:
                    if not it.future.done():
                        it.future.set_exception(exc)
                return
            COMPUTE.labels(self.name).observe(time.perf_counter() - started)
            self.stats.calls += 1
            self.stats.items += len(batch)
            for it, v in zip(batch, vectors, strict=True):
                if not it.future.done():
                    it.future.set_result(v)
        finally:
            self._sem.release()
