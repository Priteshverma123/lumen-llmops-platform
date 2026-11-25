from datetime import UTC, datetime

from lumen.pricing.catalog import PriceCatalog
from lumen.telemetry.otlp import decode_json, normalize
from lumen.telemetry.semconv import disjoint_input, map_attributes

CATALOG = PriceCatalog.from_dicts([{"version": "t1", "effective_from": "2026-01-01", "models": [
    {"provider": "anthropic", "match": "claude-sonnet-5-5.*", "input": 2.0, "output": 10.0, "cache_read": 0.1}]}])


def test_new_and_legacy_names_map_to_same_columns():
    new = map_attributes({"gen_ai.provider.name": "openai", "gen_ai.usage.input_tokens": 10})
    old = map_attributes({"gen_ai.system": "openai", "gen_ai.usage.prompt_tokens": 10})
    oi = map_attributes({"llm.provider": "openai", "llm.token_count.prompt": "10"})
    assert new.strings["provider"] == old.strings["provider"] == oi.strings["provider"] == "openai"
    assert new.ints["input_tokens"] == old.ints["input_tokens"] == oi.ints["input_tokens"] == 10


def test_new_name_wins_over_legacy():
    m = map_attributes({"gen_ai.provider.name": "anthropic", "gen_ai.system": "legacy"})
    assert m.strings["provider"] == "anthropic"


def test_cached_tokens_are_not_double_counted():
    assert disjoint_input(1000, 800, 0, already_disjoint=False) == 200
    assert disjoint_input(200, 800, 0, already_disjoint=True) == 200
    assert disjoint_input(100, 800, 0, already_disjoint=False) == 0  # misreporting producer: clamp


def _otlp(attrs: dict) -> dict:
    kv = [{"key": k, "value": {"intValue": str(v)} if isinstance(v, int) else {"stringValue": v}}
          for k, v in attrs.items()]
    span = {"traceId": "0af7651916cd43dd8448eb211c80319c", "spanId": "b7ad6b7169203331", "name": "chat", "kind": 3,
            "startTimeUnixNano": "1793000000000000000", "endTimeUnixNano": "1793000001500000000",
            "status": {"code": 1}, "attributes": kv}
    resource = {"attributes": [{"key": "service.name", "value": {"stringValue": "atlas"}}]}
    return {"resourceSpans": [{"resource": resource, "scopeSpans": [{"spans": [span]}]}]}


def test_decode_and_normalize_computes_cost_and_duration():
    spans = decode_json(_otlp({"gen_ai.provider.name": "anthropic", "gen_ai.response.model": "claude-sonnet-5-5",
                               "gen_ai.usage.input_tokens": 1_000_000, "gen_ai.usage.output_tokens": 100_000,
                               "gen_ai.usage.cache_read.input_tokens": 500_000, "gen_ai.prompt.name": "rag-answer",
                               "gen_ai.prompt.version": 3}))
    row = normalize(spans, "p1", CATALOG, capture_content=False)[0]
    assert row["service"] == "atlas" and row["duration_ms"] == 1500.0
    assert row["input_tokens"] == 500_000  # cached tokens subtracted
    # 0.5M*2 + 0.1M*10 + 0.5M*0.1 = 1.0 + 1.0 + 0.05
    assert abs(row["cost_usd"] - 2.05) < 1e-9 and row["cost_source"] == "inferred"
    assert row["prompt_name"] == "rag-answer" and row["prompt_version"] == 3
    assert row["input_preview"] == ""  # content capture is off by default


def test_client_cost_wins_and_non_llm_spans_have_no_cost():
    spans = decode_json(_otlp({"gen_ai.provider.name": "anthropic", "gen_ai.request.model": "claude-sonnet-5-5",
                               "gen_ai.usage.input_tokens": 10, "lumen.cost_usd": "0.5"}))
    assert normalize(spans, "p", CATALOG, capture_content=False)[0]["cost_source"] == "client"
    plain = normalize(decode_json(_otlp({"http.route": "/x"})), "p", CATALOG, capture_content=False)[0]
    assert plain["is_llm"] == 0 and plain["cost_usd"] == 0.0


def test_base64_ids_are_converted_to_hex():
    body = _otlp({})
    body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["traceId"] = "CvdlGRbNQ92ESOshHIAxnA=="
    assert decode_json(body)[0].trace_id == "0af7651916cd43dd8448eb211c80319c"


def test_manifest_selected_by_span_time():
    m = [{"provider": "x", "match": "m", "input": 1, "output": 1}]
    m2 = [{"provider": "x", "match": "m", "input": 2, "output": 2}]
    cat = PriceCatalog.from_dicts([{"version": "old", "effective_from": "2026-01-01", "models": m},
                                   {"version": "new", "effective_from": "2026-06-01", "models": m2}])
    early = cat.cost(provider="x", model="m", ts=datetime(2026, 3, 1, tzinfo=UTC), input_tokens=1_000_000,
                     output_tokens=0)
    late = cat.cost(provider="x", model="m", ts=datetime(2026, 7, 1, tzinfo=UTC), input_tokens=1_000_000,
                    output_tokens=0)
    assert (early.usd, early.price_version) == (1.0, "old")
    assert (late.usd, late.price_version) == (2.0, "new")
