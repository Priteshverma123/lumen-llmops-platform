# ADR-0001: ClickHouse for telemetry, Postgres for state

**Status:** accepted · 2026-11-15

## Context
LLM telemetry is append-heavy, wide, and queried with aggregations over time ranges (cost by tenant, p95 by route). Prompt registry, experiments and datasets need transactions and constraints.

## Decision
Spans, scores and embedding request logs go to ClickHouse (columnar, compressed, fast GROUP BY, TTLs). Everything transactional goes to Postgres. Langfuse made the same split (and was acquired by ClickHouse in Jan 2026).

## Consequences
+ Months of dashboards come from rollups in milliseconds. − Two datastores to operate. Mitigated with managed offerings in production and one compose file locally.
