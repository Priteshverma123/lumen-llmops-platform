# ADR-0004: Versioned semconv mapping layer

**Status:** accepted

## Context
OTel GenAI conventions are at Development stability and moved repos in v1.42. Producers emit new, legacy, and OpenInference names.

## Decision
A single mapping table (priority-ordered candidates per column) normalises at ingest. Each row stores the mapping version. Token buckets are made disjoint (cached tokens subtracted) before pricing.

## Consequences
+ Spec churn becomes a code change, not a data migration. − The mapping must track the spec. Covered by unit tests for each spelling.
