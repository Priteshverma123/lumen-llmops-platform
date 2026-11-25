"""OTLP load generator: synthetic GenAI spans → Lumen ingest, to measure sustained spans/s and
ingest-to-query freshness.

    python bench/span_load.py --url http://localhost:8000 --key lmn_... --rate 5000 --seconds 60

Freshness: a marker trace id is sent at the end; the script polls /v1/traces/<id> and reports
how long until it is queryable.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import time

import httpx

MODELS = [("anthropic", "claude-sonnet-5-5"), ("anthropic", "claude-haiku-5-5"), ("openai", "gpt-5-mini")]
ROUTES = ["answer", "fast", "deep"]


def span(trace_id: str) -> dict:
    prov, model = random.choice(MODELS)
    start = time.time_ns()
    dur = int(random.lognormvariate(6.5, 0.6) * 1e6)
    kv = {"gen_ai.provider.name": prov, "gen_ai.response.model": model, "gen_ai.operation.name": "chat",
          "gen_ai.usage.input_tokens": random.randint(500, 8000), "gen_ai.usage.output_tokens": random.randint(50, 800),
          "gen_ai.usage.cache_read.input_tokens": random.randint(0, 400), "lumen.route": random.choice(ROUTES),
          "lumen.tenant": f"tenant-{random.randint(1, 20)}", "user.id": f"user-{random.randint(1, 2000)}",
          "gen_ai.conversation.id": f"s-{random.randint(1, 5000)}", "gen_ai.prompt.name": "rag-answer",
          "gen_ai.prompt.version": random.choice([3, 4])}
    attrs = [{"key": k, "value": {"intValue": str(v)} if isinstance(v, int) else {"stringValue": v}}
             for k, v in kv.items()]
    return {"traceId": trace_id, "spanId": os.urandom(8).hex(), "name": "chat " + model, "kind": 3,
            "startTimeUnixNano": str(start), "endTimeUnixNano": str(start + dur),
            "status": {"code": 2 if random.random() < 0.01 else 1}, "attributes": attrs}


def batch(n: int, trace_id: str | None = None) -> dict:
    spans = [span(trace_id or os.urandom(16).hex()) for _ in range(n)]
    return {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name",
                                                             "value": {"stringValue": "loadgen"}}]},
                               "scopeSpans": [{"spans": spans}]}]}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--key", required=True)
    ap.add_argument("--rate", type=int, default=2000, help="spans per second")
    ap.add_argument("--seconds", type=int, default=30)
    ap.add_argument("--batch", type=int, default=500)
    a = ap.parse_args()
    headers = {"Authorization": f"Bearer {a.key}", "Content-Type": "application/json"}
    sent = rejected = 0
    async with httpx.AsyncClient(base_url=a.url, headers=headers, timeout=30) as http:
        t_end = time.perf_counter() + a.seconds
        interval = a.batch / a.rate
        while time.perf_counter() < t_end:
            t0 = time.perf_counter()
            r = await http.post("/v1/traces", json=batch(a.batch))
            if r.status_code == 200:
                sent += a.batch
            else:
                rejected += a.batch
            await asyncio.sleep(max(0.0, interval - (time.perf_counter() - t0)))
        marker = os.urandom(16).hex()
        await http.post("/v1/traces", json=batch(1, marker))
        t0 = time.perf_counter()
        while (await http.get(f"/v1/traces/{marker}")).status_code != 200:
            await asyncio.sleep(0.25)
        fresh = time.perf_counter() - t0
    print(f"sent={sent} rejected(429)={rejected} achieved={sent / a.seconds:.0f} spans/s freshness={fresh:.2f}s")


if __name__ == "__main__":
    asyncio.run(main())
