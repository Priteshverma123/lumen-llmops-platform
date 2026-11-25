"""Versioned semantic-convention mapping: OTel attributes → Lumen columns.

Why a mapping layer: the GenAI conventions are still at *Development* stability and moved repos
in v1.42 (Jun 2026). Producers in the wild emit the new names, the legacy names
(``gen_ai.system``, ``gen_ai.usage.prompt_tokens``) and OpenInference (``llm.*``). Ingest accepts
all of them and normalises once; each row records which mapping version produced it, so a
future spec change is a new mapping version, not a data migration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MAPPING_VERSION = "genai-2026.06"

# column → candidate attribute names, highest priority first
STRING_FIELDS: dict[str, tuple[str, ...]] = {
    "operation": ("gen_ai.operation.name",),
    "provider": ("gen_ai.provider.name", "gen_ai.system", "llm.provider", "llm.system"),
    "request_model": ("gen_ai.request.model", "llm.model_name", "llm.request.model"),
    "response_model": ("gen_ai.response.model", "llm.response.model"),
    "session_id": ("gen_ai.conversation.id", "session.id", "langfuse.session.id"),
    "user_id": ("user.id", "enduser.id", "langfuse.user.id"),
    "prompt_name": ("gen_ai.prompt.name", "lumen.prompt.name"),
    "experiment_id": ("lumen.experiment.id",),
    "variant": ("lumen.experiment.variant",),
    "route": ("lumen.route", "atlas.route"),
    "tenant": ("lumen.tenant", "atlas.tenant", "tenant.id"),
    "environment": ("deployment.environment.name", "deployment.environment"),
    "cache": ("lumen.cache", "atlas.cache"),
}
INT_FIELDS: dict[str, tuple[str, ...]] = {
    "input_tokens": ("gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens", "llm.token_count.prompt"),
    "output_tokens": ("gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens", "llm.token_count.completion"),
    "cache_read_tokens": ("gen_ai.usage.cache_read.input_tokens", "gen_ai.usage.cache_read_input_tokens",
                          "llm.token_count.prompt_details.cache_read"),
    "cache_write_tokens": ("gen_ai.usage.cache_write.input_tokens", "gen_ai.usage.cache_creation_input_tokens",
                           "llm.token_count.prompt_details.cache_write"),
    "reasoning_tokens": ("gen_ai.usage.reasoning.output_tokens",),
    "prompt_version": ("gen_ai.prompt.version", "lumen.prompt.version"),
}
FLOAT_FIELDS: dict[str, tuple[str, ...]] = {
    "client_cost_usd": ("lumen.cost_usd", "gen_ai.usage.cost", "llm.cost.total"),
    "ttft_ms": ("lumen.ttft_ms", "gen_ai.response.time_to_first_chunk_ms"),
}
INPUT_CONTENT = ("gen_ai.input.messages", "gen_ai.prompt", "input.value")
OUTPUT_CONTENT = ("gen_ai.output.messages", "gen_ai.completion", "output.value")
FINISH = ("gen_ai.response.finish_reasons",)
# Producers that already report non-overlapping buckets (input excludes cached) set this to true.
DISJOINT_FLAG = "lumen.usage.disjoint"


@dataclass(slots=True)
class Mapped:
    strings: dict[str, str]
    ints: dict[str, int]
    floats: dict[str, float]
    input_text: str | None
    output_text: str | None
    finish_reason: str
    disjoint: bool


def _first(attrs: dict[str, Any], names: tuple[str, ...]) -> Any:
    for n in names:
        if n in attrs and attrs[n] not in (None, ""):
            return attrs[n]
    return None


def _to_int(v: Any) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _to_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def map_attributes(attrs: dict[str, Any]) -> Mapped:
    strings = {col: str(v) for col, names in STRING_FIELDS.items() if (v := _first(attrs, names)) is not None}
    ints = {col: _to_int(_first(attrs, names)) for col, names in INT_FIELDS.items()}
    floats: dict[str, float] = {}
    for col, names in FLOAT_FIELDS.items():
        f = _to_float(_first(attrs, names))
        if f is not None:
            floats[col] = f
    fin = _first(attrs, FINISH)
    if isinstance(fin, list):
        fin = fin[0] if fin else ""
    inp, out = _first(attrs, INPUT_CONTENT), _first(attrs, OUTPUT_CONTENT)
    return Mapped(strings=strings, ints=ints, floats=floats,
                  input_text=None if inp is None else str(inp), output_text=None if out is None else str(out),
                  finish_reason=str(fin or ""), disjoint=str(attrs.get(DISJOINT_FLAG, "")).lower() == "true")


def disjoint_input(input_tokens: int, cache_read: int, cache_write: int, already_disjoint: bool) -> int:
    """Per the semconv, input_tokens INCLUDES cached tokens. Subtract them so cost buckets never
    overlap (the classic double-counting bug); clamp at 0 for producers that misreport."""
    if already_disjoint:
        return input_tokens
    return max(0, input_tokens - cache_read - cache_write)
