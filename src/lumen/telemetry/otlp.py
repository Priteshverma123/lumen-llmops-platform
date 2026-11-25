"""OTLP/HTTP trace decoding (JSON and protobuf) → flat span records → ClickHouse rows.

OTLP/JSON encodes trace/span ids as hex strings and 64-bit ints as strings; protobuf carries raw
bytes. Both paths converge on ``RawSpan`` so normalisation is written once.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from lumen.pricing.catalog import PriceCatalog
from lumen.telemetry.semconv import MAPPING_VERSION, disjoint_input, map_attributes

STATUS = {0: "unset", 1: "ok", 2: "error"}
KIND = {0: "unspecified", 1: "internal", 2: "server", 3: "client", 4: "producer", 5: "consumer"}


@dataclass(slots=True)
class RawSpan:
    trace_id: str
    span_id: str
    parent_span_id: str
    name: str
    kind: int
    start_ns: int
    end_ns: int
    status_code: int
    status_message: str
    attributes: dict[str, Any]
    resource: dict[str, Any]
    events: list[dict[str, Any]] = field(default_factory=list)


# ----------------------------------------------------------------------------- attribute values
def _json_value(v: dict[str, Any]) -> Any:
    if "stringValue" in v:
        return v["stringValue"]
    if "intValue" in v:
        return int(v["intValue"])
    if "doubleValue" in v:
        return float(v["doubleValue"])
    if "boolValue" in v:
        return bool(v["boolValue"])
    if "arrayValue" in v:
        return [_json_value(x) for x in v["arrayValue"].get("values", [])]
    if "kvlistValue" in v:
        return {kv["key"]: _json_value(kv.get("value", {})) for kv in v["kvlistValue"].get("values", [])}
    if "bytesValue" in v:
        return v["bytesValue"]
    return None


def _json_attrs(items: list[dict[str, Any]] | None) -> dict[str, Any]:
    return {kv["key"]: _json_value(kv.get("value", {})) for kv in items or []}


def _id(value: str | None, size: int) -> str:
    """OTLP/JSON spec says hex; some exporters send base64. Accept both."""
    if not value:
        return ""
    if len(value) == size * 2:
        return value.lower()
    try:
        return base64.b64decode(value).hex()
    except (ValueError, TypeError):
        return value


def decode_json(body: dict[str, Any]) -> list[RawSpan]:
    out: list[RawSpan] = []
    for rs in body.get("resourceSpans", []):
        resource = _json_attrs(rs.get("resource", {}).get("attributes"))
        for ss in rs.get("scopeSpans", []) or rs.get("instrumentationLibrarySpans", []):
            for sp in ss.get("spans", []):
                st = sp.get("status") or {}
                code = st.get("code", 0)
                if isinstance(code, str):  # enum name form
                    code = {"STATUS_CODE_OK": 1, "STATUS_CODE_ERROR": 2}.get(code, 0)
                out.append(RawSpan(
                    trace_id=_id(sp.get("traceId"), 16), span_id=_id(sp.get("spanId"), 8),
                    parent_span_id=_id(sp.get("parentSpanId"), 8), name=sp.get("name", ""),
                    kind=int(sp.get("kind", 0)) if not isinstance(sp.get("kind"), str) else 0,
                    start_ns=int(sp.get("startTimeUnixNano", 0)), end_ns=int(sp.get("endTimeUnixNano", 0)),
                    status_code=int(code), status_message=st.get("message", ""),
                    attributes=_json_attrs(sp.get("attributes")), resource=resource,
                    events=[{"name": e.get("name", ""), "attributes": _json_attrs(e.get("attributes"))}
                            for e in sp.get("events", [])]))
    return out


def _pb_value(v: Any) -> Any:
    which = v.WhichOneof("value")
    if which == "array_value":
        return [_pb_value(x) for x in v.array_value.values]
    if which == "kvlist_value":
        return {kv.key: _pb_value(kv.value) for kv in v.kvlist_value.values}
    if which == "bytes_value":
        return v.bytes_value.hex()
    return getattr(v, which) if which else None


def decode_protobuf(data: bytes) -> list[RawSpan]:
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    req = ExportTraceServiceRequest()
    req.ParseFromString(data)
    out: list[RawSpan] = []
    for rs in req.resource_spans:
        resource = {kv.key: _pb_value(kv.value) for kv in rs.resource.attributes}
        for ss in rs.scope_spans:
            for sp in ss.spans:
                out.append(RawSpan(
                    trace_id=sp.trace_id.hex(), span_id=sp.span_id.hex(), parent_span_id=sp.parent_span_id.hex(),
                    name=sp.name, kind=int(sp.kind), start_ns=sp.start_time_unix_nano, end_ns=sp.end_time_unix_nano,
                    status_code=int(sp.status.code), status_message=sp.status.message,
                    attributes={kv.key: _pb_value(kv.value) for kv in sp.attributes}, resource=resource,
                    events=[{"name": e.name, "attributes": {kv.key: _pb_value(kv.value) for kv in e.attributes}}
                            for e in sp.events]))
    return out


# ----------------------------------------------------------------------------- normalisation
def _ts(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1e9, tz=UTC)


def normalize(spans: list[RawSpan], project_id: str, catalog: PriceCatalog, *, capture_content: bool,
              content_max: int = 2000) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for s in spans:
        attrs = {**s.resource, **s.attributes}  # span attrs override resource attrs
        m = map_attributes(attrs)
        start = _ts(s.start_ns) if s.start_ns else datetime.now(UTC)
        end = _ts(s.end_ns) if s.end_ns else start
        provider = m.strings.get("provider", "")
        model = m.strings.get("response_model") or m.strings.get("request_model", "")
        cr, cw = m.ints.get("cache_read_tokens", 0), m.ints.get("cache_write_tokens", 0)
        inp = disjoint_input(m.ints.get("input_tokens", 0), cr, cw, m.disjoint)
        out_tok = m.ints.get("output_tokens", 0)
        is_llm = bool(provider or model or inp or out_tok)
        cost = catalog.cost(provider=provider, model=model, ts=start, input_tokens=inp, output_tokens=out_tok,
                            cache_read=cr, cache_write=cw, reasoning=m.ints.get("reasoning_tokens", 0),
                            client_cost=m.floats.get("client_cost_usd")) if is_llm else None
        # evaluation results emitted as span events by producers (gen_ai.evaluation.result)
        evals = [e for e in s.events if e["name"] == "gen_ai.evaluation.result"]
        known = {k for k in attrs if k.startswith(("gen_ai.", "llm.", "lumen."))}
        rows.append({
            "project_id": project_id, "trace_id": s.trace_id, "span_id": s.span_id,
            "parent_span_id": s.parent_span_id, "start_time": start, "end_time": end,
            "duration_ms": max(0.0, (s.end_ns - s.start_ns) / 1e6) if s.end_ns and s.start_ns else 0.0,
            "name": s.name, "kind": KIND.get(s.kind, "unspecified"), "status": STATUS.get(s.status_code, "unset"),
            "status_message": s.status_message[:1000], "service": str(s.resource.get("service.name", "")),
            "operation": m.strings.get("operation", ""), "provider": provider,
            "request_model": m.strings.get("request_model", ""), "response_model": m.strings.get("response_model", ""),
            "route": m.strings.get("route", ""), "tenant": m.strings.get("tenant", ""),
            "session_id": m.strings.get("session_id", ""), "user_id": m.strings.get("user_id", ""),
            "environment": m.strings.get("environment", ""), "prompt_name": m.strings.get("prompt_name", ""),
            "prompt_version": m.ints.get("prompt_version", 0), "experiment_id": m.strings.get("experiment_id", ""),
            "variant": m.strings.get("variant", ""), "cache": m.strings.get("cache", ""),
            "input_tokens": inp, "output_tokens": out_tok, "cache_read_tokens": cr, "cache_write_tokens": cw,
            "reasoning_tokens": m.ints.get("reasoning_tokens", 0),
            "cost_usd": cost.usd if cost else 0.0, "cost_source": cost.source if cost else "",
            "price_version": cost.price_version if cost else "", "ttft_ms": m.floats.get("ttft_ms", 0.0),
            "finish_reason": m.finish_reason, "is_llm": int(is_llm),
            "input_preview": (m.input_text or "")[:content_max] if capture_content else "",
            "output_preview": (m.output_text or "")[:content_max] if capture_content else "",
            "attributes": {k: str(v)[:500] for k, v in s.attributes.items() if k not in known
                           and k not in ("gen_ai.input.messages", "gen_ai.output.messages")},
            "eval_events": [{"name": str(e["attributes"].get("gen_ai.evaluation.name", "")),
                             "value": e["attributes"].get("gen_ai.evaluation.score.value"),
                             "label": str(e["attributes"].get("gen_ai.evaluation.score.label", "")),
                             "comment": str(e["attributes"].get("gen_ai.evaluation.explanation", ""))[:1000]}
                            for e in evals],
            "semconv_version": MAPPING_VERSION,
        })
    return rows
