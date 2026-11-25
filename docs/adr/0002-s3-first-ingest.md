# ADR-0002: S3-first ingest with a Redis stream

**Status:** accepted

## Context
Ingest must not lose data when ClickHouse or a worker is down, and must not block producers.

## Decision
The API writes the raw OTLP batch to object storage, then XADDs a pointer to a Redis stream and returns 200. A consumer group inserts in large batches and XACKs only after success. Inserts carry dedup tokens. Kafka was rejected as heavier to self-host (Langfuse made the same call).

## Consequences
+ Durable, replayable, horizontally scalable, with backpressure via 429. − An extra hop of latency (seconds). Acceptable for observability, which has a freshness SLO < 10 s.
