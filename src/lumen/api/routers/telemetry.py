"""OTLP ingest, traces/sessions explorer, metrics, scores and feedback."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, Query, Request, Response
from pydantic import BaseModel, Field

from lumen.api.deps import Ingest, Read, Write
from lumen.common.errors import InvalidInput, NotFound
from lumen.ingest.service import accept_batch
from lumen.storage.clickhouse import SCORE_COLUMNS, ClickHouse

router = APIRouter(tags=["telemetry"])


def ch(request: Request) -> ClickHouse:
    return request.app.state.ch  # type: ignore[no-any-return]


def _range(frm: datetime | None, to: datetime | None, default_hours: int = 24) -> dict[str, datetime]:
    to = (to or datetime.now(UTC)).astimezone(UTC).replace(tzinfo=None)
    frm = (frm.astimezone(UTC).replace(tzinfo=None) if frm else to - timedelta(hours=default_hours))
    if frm >= to:
        raise InvalidInput("'from' must be before 'to'")
    return {"frm": frm, "to": to}


# ----------------------------------------------------------------------------- OTLP ingest
@router.post("/v1/traces", summary="OTLP/HTTP trace export (protobuf or JSON)")
async def otlp_traces(request: Request, p: Ingest, content_type: Annotated[str, Header()] = "application/json",
                      content_encoding: Annotated[str | None, Header()] = None) -> Response:
    s = request.app.state.settings
    await accept_batch(request.app.state.redis, request.app.state.objects, project_id=str(p.project_id),
                       body=await request.body(), content_type=content_type, content_encoding=content_encoding,
                       max_bytes=s.ingest_max_body_mb * 1024 * 1024)
    # ExportTraceServiceResponse with no partial_success: empty protobuf / empty JSON object
    if "protobuf" in content_type:
        return Response(b"", media_type="application/x-protobuf")
    return Response(b"{}", media_type="application/json")


# ----------------------------------------------------------------------------- traces
@router.get("/v1/traces")
async def list_traces(request: Request, p: Read, frm: Annotated[datetime | None, Query(alias="from")] = None,
                      to: datetime | None = None, route: str = "", user_id: str = "", session_id: str = "",
                      status: Literal["", "ok", "error"] = "", model: str = "", limit: int = Query(50, le=500)
                      ) -> list[dict[str, Any]]:
    params = {"p": str(p.project_id), "route": route, "user": user_id, "sess": session_id, "status": status,
              "model": model, "limit": limit, **_range(frm, to)}
    return await ch(request).query("""
        SELECT trace_id, min(start_time) AS start, max(end_time) AS end,
               dateDiff('millisecond', min(start_time), max(end_time)) AS duration_ms,
               argMin(name, start_time) AS root_name, any(user_id) AS user_id, any(session_id) AS session_id,
               count() AS spans, countIf(status = 'error') AS errors, sum(cost_usd) AS cost_usd,
               sum(input_tokens + cache_read_tokens) AS input_tokens, sum(output_tokens) AS output_tokens,
               groupUniqArrayIf(response_model, response_model != '') AS models
        FROM spans
        WHERE project_id = {p:String} AND start_time BETWEEN {frm:DateTime64(6)} AND {to:DateTime64(6)}
          AND ({route:String} = '' OR route = {route:String})
          AND ({user:String} = '' OR user_id = {user:String})
          AND ({sess:String} = '' OR session_id = {sess:String})
          AND ({model:String} = '' OR response_model = {model:String} OR request_model = {model:String})
        GROUP BY trace_id
        HAVING {status:String} = '' OR ({status:String} = 'error' AND errors > 0) OR ({status:String} = 'ok' AND errors = 0)
        ORDER BY start DESC LIMIT {limit:UInt32}""", params)


@router.get("/v1/traces/{trace_id}")
async def get_trace(trace_id: str, request: Request, p: Read) -> dict[str, Any]:
    params = {"p": str(p.project_id), "t": trace_id.lower()}
    spans = await ch(request).query("""
        SELECT * EXCEPT (attributes), toString(attributes) AS attributes_json FROM spans
        WHERE project_id = {p:String} AND trace_id = {t:String} ORDER BY start_time LIMIT 5000""", params)
    if not spans:
        raise NotFound("trace not found")
    scores = await ch(request).query("""
        SELECT score_id, span_id, name, data_type, argMax(value, updated_at) AS value,
               argMax(string_value, updated_at) AS string_value, argMax(source, updated_at) AS source,
               argMax(comment, updated_at) AS comment
        FROM scores WHERE project_id = {p:String} AND trace_id = {t:String}
        GROUP BY score_id, span_id, name, data_type""", params)
    return {"trace_id": trace_id, "spans": waterfall(spans), "scores": scores,
            "totals": {"cost_usd": sum(s["cost_usd"] for s in spans),
                       "tokens": sum(s["input_tokens"] + s["output_tokens"] + s["cache_read_tokens"] for s in spans)}}


def waterfall(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Adds depth + offset_ms (from trace start) so a UI can draw the waterfall directly."""
    t0 = min(s["start_time"] for s in spans)
    by_id = {s["span_id"]: s for s in spans}

    def depth(s: dict[str, Any], seen: int = 0) -> int:
        parent = by_id.get(s["parent_span_id"])
        return 0 if parent is None or seen > 64 else 1 + depth(parent, seen + 1)

    for s in spans:
        s["depth"] = depth(s)
        s["offset_ms"] = (s["start_time"] - t0).total_seconds() * 1000
    return spans


@router.get("/v1/sessions")
async def list_sessions(request: Request, p: Read, frm: Annotated[datetime | None, Query(alias="from")] = None,
                        to: datetime | None = None, user_id: str = "", limit: int = Query(50, le=500)
                        ) -> list[dict[str, Any]]:
    return await ch(request).query("""
        SELECT session_id, any(user_id) AS user_id, uniqExact(trace_id) AS traces, min(start_time) AS first_seen,
               max(end_time) AS last_seen, sum(cost_usd) AS cost_usd
        FROM spans WHERE project_id = {p:String} AND session_id != ''
          AND start_time BETWEEN {frm:DateTime64(6)} AND {to:DateTime64(6)}
          AND ({user:String} = '' OR user_id = {user:String})
        GROUP BY session_id ORDER BY last_seen DESC LIMIT {limit:UInt32}""",
                                   {"p": str(p.project_id), "user": user_id, "limit": limit, **_range(frm, to, 24 * 7)})


# ----------------------------------------------------------------------------- metrics
GROUPS = {"route": "route", "model": "model", "tenant": "tenant", "provider": "provider",
          "prompt": "concat(prompt_name, '@v', toString(prompt_version))", "variant": "variant"}


@router.get("/v1/metrics/llm")
async def llm_metrics(request: Request, p: Read, group_by: Literal["route", "model", "tenant", "provider", "prompt",
                                                                   "variant"] = "route",
                      interval: Literal["minute", "hour", "day"] = "hour",
                      frm: Annotated[datetime | None, Query(alias="from")] = None, to: datetime | None = None
                      ) -> list[dict[str, Any]]:
    """Cost, tokens, latency percentiles, error and cache rates from the rollup tables."""
    table = "llm_minute" if interval == "minute" else "llm_hour"
    bucket = {"minute": "minute", "hour": "minute", "day": "toStartOfDay(minute)"}[interval]
    return await ch(request).query(f"""
        SELECT {bucket} AS t, {GROUPS[group_by]} AS key, sum(calls) AS calls, sum(errors) / sum(calls) AS error_rate,
               sum(cache_hits) / sum(calls) AS cache_hit_rate, sum(cost_usd) AS cost_usd,
               sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens,
               sum(cache_read_tokens) / greatest(1, sum(input_tokens) + sum(cache_read_tokens)) AS cache_read_ratio,
               quantilesTDigestMerge(0.5, 0.95, 0.99)(latency) AS latency_ms_p50_p95_p99,
               quantilesTDigestMerge(0.5, 0.95)(ttft) AS ttft_ms_p50_p95, uniqMerge(users) AS users
        FROM {table}
        WHERE project_id = {{p:String}} AND minute BETWEEN {{frm:DateTime}} AND {{to:DateTime}}
        GROUP BY t, key ORDER BY t, cost_usd DESC""", {"p": str(p.project_id), **_range(frm, to)})


# ----------------------------------------------------------------------------- scores & feedback
class ScoreIn(BaseModel):
    trace_id: str = Field(min_length=32, max_length=32)
    span_id: str = ""
    session_id: str = ""
    name: str = Field(min_length=1, max_length=100)
    data_type: Literal["numeric", "categorical", "boolean"] = "numeric"
    value: float | None = None
    string_value: str = ""
    comment: str = Field("", max_length=4000)
    source: Literal["api", "eval", "annotation", "feedback"] = "api"
    score_id: str | None = Field(None, description="idempotency key; re-posting the same id updates the score")


async def write_scores(c: ClickHouse, project_id: uuid.UUID, scores: list[ScoreIn], author: str) -> list[str]:
    now = datetime.now(UTC)
    rows = []
    for sc in scores:
        if sc.data_type == "boolean" and sc.value not in (0.0, 1.0):
            raise InvalidInput("boolean scores need value 0 or 1")
        if sc.data_type == "categorical" and not sc.string_value:
            raise InvalidInput("categorical scores need string_value")
        rows.append({"project_id": str(project_id), "score_id": sc.score_id or uuid.uuid4().hex,
                     "trace_id": sc.trace_id.lower(), "span_id": sc.span_id, "session_id": sc.session_id,
                     "name": sc.name, "data_type": sc.data_type, "value": sc.value or 0.0,
                     "string_value": sc.string_value, "source": sc.source, "comment": sc.comment, "author": author,
                     "config_id": "", "created_at": now})
    await c.insert_rows("scores", SCORE_COLUMNS, rows, batch_id=uuid.uuid4().hex)
    return [r["score_id"] for r in rows]


@router.post("/v1/scores", status_code=201)
async def create_scores(body: list[ScoreIn], request: Request, p: Write) -> dict[str, list[str]]:
    if len(body) > 1000:
        raise InvalidInput("max 1000 scores per request")
    return {"score_ids": await write_scores(ch(request), p.project_id, body, p.key_id)}


class FeedbackIn(BaseModel):
    trace_id: str = Field(min_length=32, max_length=32)
    rating: Literal[-1, 1]
    comment: str = ""
    user_id: str = ""


@router.post("/v1/feedback", status_code=201)
async def feedback(body: FeedbackIn, request: Request, p: Ingest) -> dict[str, list[str]]:
    """End-user thumbs. Uses the ingest scope so client apps can send it with their telemetry key."""
    score = ScoreIn(trace_id=body.trace_id, name="feedback", data_type="numeric",
                    value=1.0 if body.rating > 0 else 0.0, comment=body.comment, source="feedback",
                    score_id=f"fb:{body.trace_id}:{body.user_id}")
    return {"score_ids": await write_scores(ch(request), p.project_id, [score], body.user_id or p.key_id)}
