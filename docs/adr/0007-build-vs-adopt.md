# ADR-0007: Build vs adopt (Langfuse, Phoenix, Opik)

**Status:** accepted

## Context
Mature open-source LLMOps platforms exist (Langfuse MIT core, Phoenix ELv2, Opik Apache-2.0).

## Decision
Lumen is built to own and demonstrate the engineering underneath: ingest durability, ClickHouse modelling, cost attribution, experiment statistics, embedding serving. It stays interoperable because it is OTLP-native: the same traces can be sent to Langfuse by changing the collector exporter. No code is copied from source-available projects (Phoenix ELv2, Infinity v3).

## Consequences
+ Deep understanding, a focused feature set. − No UI parity with Langfuse. Grafana dashboards-as-code cover the operational views.
