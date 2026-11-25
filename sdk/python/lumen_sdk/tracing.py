"""Tag the current OTel span so Lumen can attribute cost/latency/quality to a prompt version and
experiment variant. Uses the GenAI semconv names (``gen_ai.prompt.name/version``) plus Lumen's
experiment attributes."""

from __future__ import annotations

from opentelemetry import trace


def record_prompt(name: str, version: int) -> None:
    span = trace.get_current_span()
    span.set_attribute("gen_ai.prompt.name", name)
    span.set_attribute("gen_ai.prompt.version", version)


def record_experiment(key: str, variant: str) -> None:
    span = trace.get_current_span()
    span.set_attribute("lumen.experiment.id", key)
    span.set_attribute("lumen.experiment.variant", variant)
