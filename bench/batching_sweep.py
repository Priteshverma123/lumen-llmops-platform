"""Throughput/latency sweep for the embedding service's micro-batching window.

Fires C concurrent clients, each sending single-text requests (the worst case for TEI's FIFO
batching), against the running embedder for several ``max_wait_ms`` values (restart the
embedder with LUMEN_EMBED_MAX_WAIT_MS=<v> per run, or pass --direct to run against TEI as the
unbatched baseline). Use --no-cache-texts so every text is unique (measures compute, not cache).

    python bench/batching_sweep.py --url http://localhost:8080 --clients 64 --seconds 30 --label wait=5ms
    python bench/batching_sweep.py --url http://localhost:8081 --direct --label tei-direct

Appends a row to bench/results/batching.md: label, req/s, p50/p95/p99 latency.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
import uuid
from pathlib import Path

import httpx


async def client(url: str, direct: bool, deadline: float, lat: list[float]) -> None:
    async with httpx.AsyncClient(timeout=60) as http:
        while time.perf_counter() < deadline:
            text = f"benchmark sentence {uuid.uuid4().hex} about embeddings and batching"
            t0 = time.perf_counter()
            if direct:
                r = await http.post(f"{url}/embed", json={"inputs": [text]})
            else:
                r = await http.post(f"{url}/v1/embed", json={"inputs": [text], "input_type": "query"})
            r.raise_for_status()
            lat.append((time.perf_counter() - t0) * 1000)


def pct(xs: list[float], p: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(p / 100 * len(s)))]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8080")
    ap.add_argument("--clients", type=int, default=64)
    ap.add_argument("--seconds", type=int, default=30)
    ap.add_argument("--direct", action="store_true", help="hit TEI directly (unbatched baseline)")
    ap.add_argument("--label", default="run")
    a = ap.parse_args()
    lat: list[float] = []
    deadline = time.perf_counter() + a.seconds
    await asyncio.gather(*(client(a.url, a.direct, deadline, lat) for _ in range(a.clients)))
    rps = len(lat) / a.seconds
    row = (f"| {a.label} | {a.clients} | {rps:.1f} | {statistics.median(lat):.1f} | {pct(lat, 95):.1f} | "
           f"{pct(lat, 99):.1f} |")
    out = Path(__file__).parent / "results" / "batching.md"
    out.parent.mkdir(exist_ok=True)
    if not out.exists():
        out.write_text("| label | clients | req/s | p50 ms | p95 ms | p99 ms |\n|---|---|---|---|---|---|\n")
    with out.open("a") as f:
        f.write(row + "\n")
    print(row)


if __name__ == "__main__":
    asyncio.run(main())
