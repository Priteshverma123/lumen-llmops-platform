# ADR-0003: Wide insert-only spans table (no ReplacingMergeTree on the hot path)

**Status:** accepted

## Context
ReplacingMergeTree + FINAL and trace-to-observation joins caused memory exhaustion at scale (Langfuse v3 → v4 rewrite, 2026).

## Decision
Denormalise trace-level attributes onto every span in one MergeTree table, ordered by (project, minute, hash(trace), span). Idempotency comes from insert dedup tokens, not merges. Rollups use AggregatingMergeTree MVs.

## Consequences
+ Predictable memory, no FINAL, skip indexes work. − Updating a span is not supported. Corrections are new scores, which matches an append-only audit model.
