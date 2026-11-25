# ADR-0006: Server-side micro-batching in front of TEI

**Status:** accepted

## Context
TEI batches strictly FIFO with no wait window, so bursty single-text traffic produces tiny batches. Several services embed the same texts repeatedly.

## Decision
A central embedder adds a content-hash cache (model@version in the key), single-flight, and a token-budget micro-batcher with a few-ms wait window and query-over-document priority. It exposes TEI-compatible and OpenAI-compatible APIs so clients need no changes.

## Consequences
+ Higher throughput, fewer duplicate computations, one place for model versioning. − Adds up to max_wait_ms of latency per request, measured with bench/batching_sweep.py.
