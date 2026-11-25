import asyncio

import pytest

from lumen.common.errors import Overloaded
from lumen.embeddings.batcher import MicroBatcher
from lumen.embeddings.service import cache_key, postprocess


async def test_concurrent_requests_are_coalesced_into_few_calls():
    calls: list[int] = []

    async def backend(texts: list[str]) -> list[list[float]]:
        calls.append(len(texts))
        await asyncio.sleep(0.001)
        return [[float(len(t))] for t in texts]

    b = MicroBatcher("m", backend, max_wait_ms=20, max_batch_items=64)
    b.start()
    try:
        results = await asyncio.gather(*(b.submit([f"text {i}"]) for i in range(40)))
    finally:
        await b.stop()
    assert [r[0][0] for r in results] == [float(len(f"text {i}")) for i in range(40)]
    assert len(calls) <= 3 and sum(calls) == 40


async def test_queries_are_served_before_documents():
    order: list[str] = []

    async def backend(texts: list[str]) -> list[list[float]]:
        order.extend(texts)
        return [[0.0] for _ in texts]

    b = MicroBatcher("m", backend, max_wait_ms=30, max_batch_items=2)
    b.start()
    try:
        await asyncio.gather(b.submit(["d1", "d2", "d3"], "document"), b.submit(["q1"], "query"))
    finally:
        await b.stop()
    assert order.index("q1") <= 1


async def test_backend_errors_propagate_to_callers():
    async def backend(texts: list[str]) -> list[list[float]]:
        raise RuntimeError("boom")

    b = MicroBatcher("m", backend, max_wait_ms=1)
    b.start()
    try:
        with pytest.raises(RuntimeError):
            await b.submit(["x"])
    finally:
        await b.stop()


async def test_backpressure_when_queue_full():
    async def backend(texts: list[str]) -> list[list[float]]:
        return [[0.0] for _ in texts]

    b = MicroBatcher("m", backend, max_queue=2)
    with pytest.raises(Overloaded):
        await b.submit(["a", "b", "c"])


def test_cache_key_includes_versioned_model_and_normalises_text():
    assert cache_key("bge@1", "query", "Hello  world") == cache_key("bge@1", "query", "Hello world")
    assert cache_key("bge@1", "query", "x") != cache_key("bge@2", "query", "x")
    assert cache_key("bge@1", "query", "x") != cache_key("bge@1", "document", "x")


def test_matryoshka_truncation_renormalises_and_quantises():
    assert postprocess([3.0, 4.0, 12.0], 2, "float") == pytest.approx([0.6, 0.8])
    assert postprocess([0.5, -0.5], None, "int8") == [64, -64]
