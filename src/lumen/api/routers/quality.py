"""Datasets (+ experiment runs on datasets), annotation queues, alert rules, admin bootstrap."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_, select, update

from lumen.api.deps import Admin, Read, Write, hash_key, new_key
from lumen.api.routers.telemetry import ScoreIn, write_scores
from lumen.common.errors import Conflict, NotFound, Unauthorized
from lumen.storage.models import (
    AlertEvent,
    AlertRule,
    AnnotationItem,
    AnnotationQueue,
    ApiKey,
    Dataset,
    DatasetItem,
    DatasetRun,
    DatasetRunItem,
    Project,
    ReembedJob,
)
from lumen.storage.postgres import session

router = APIRouter(tags=["datasets", "annotations", "alerts", "admin"])


# ----------------------------------------------------------------------------- admin
class BootstrapIn(BaseModel):
    project: str = Field(min_length=1, max_length=200)


@router.post("/v1/admin/bootstrap", status_code=201)
async def bootstrap(body: BootstrapIn, request: Request, x_bootstrap_token: Annotated[str, Header()]) -> dict[str, str]:
    import hmac

    if not hmac.compare_digest(x_bootstrap_token, request.app.state.settings.bootstrap_token.get_secret_value()):
        raise Unauthorized("bad bootstrap token")
    raw = new_key()
    async with session() as s:
        proj = Project(id=uuid.uuid4(), name=body.project)
        s.add(proj)
        await s.flush()
        s.add(ApiKey(project_id=proj.id, name="bootstrap-admin", key_hash=hash_key(raw),
                     scopes=["admin", "read", "write", "ingest"]))
    return {"project_id": str(proj.id), "api_key": raw}


class KeyIn(BaseModel):
    name: str
    scopes: list[Literal["ingest", "read", "write", "admin"]] = ["ingest"]


@router.post("/v1/admin/keys", status_code=201)
async def create_key(body: KeyIn, p: Admin) -> dict[str, str]:
    raw = new_key()
    async with session() as s:
        k = ApiKey(project_id=p.project_id, name=body.name, key_hash=hash_key(raw), scopes=list(body.scopes))
        s.add(k)
    return {"api_key": raw, "key_id": str(k.id)}


# ----------------------------------------------------------------------------- datasets
class DatasetIn(BaseModel):
    name: str
    description: str = ""


class ItemIn(BaseModel):
    input: dict[str, Any]
    expected: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    source_trace_id: str | None = None


async def _dataset(s: Any, project_id: uuid.UUID, name: str) -> Dataset:
    ds = (await s.execute(select(Dataset).where(Dataset.project_id == project_id, Dataset.name == name))
          ).scalar_one_or_none()
    if ds is None:
        raise NotFound(f"dataset '{name}' not found")
    return ds  # type: ignore[no-any-return]


@router.post("/v1/datasets", status_code=201)
async def create_dataset(body: DatasetIn, p: Write) -> dict[str, str]:
    async with session() as s:
        ds = Dataset(id=uuid.uuid4(), project_id=p.project_id, name=body.name, description=body.description)
        s.add(ds)
    return {"dataset_id": str(ds.id)}


@router.post("/v1/datasets/{name}/items", status_code=201)
async def add_items(name: str, body: list[ItemIn], p: Write) -> dict[str, int]:
    """Items may be promoted from production traces (source_trace_id): the feedback → dataset loop."""
    async with session() as s:
        ds = await _dataset(s, p.project_id, name)
        s.add_all([DatasetItem(dataset_id=ds.id, input=i.input, expected=i.expected, item_metadata=i.metadata,
                               source_trace_id=i.source_trace_id) for i in body])
    return {"added": len(body)}


@router.get("/v1/datasets/{name}/items")
async def list_items(name: str, p: Read, limit: int = 500) -> list[dict[str, Any]]:
    async with session() as s:
        ds = await _dataset(s, p.project_id, name)
        rows = (await s.execute(select(DatasetItem).where(DatasetItem.dataset_id == ds.id).limit(min(limit, 5000))))
        return [{"id": str(i.id), "input": i.input, "expected": i.expected, "metadata": i.item_metadata,
                 "source_trace_id": i.source_trace_id} for i in rows.scalars()]


class RunItemIn(BaseModel):
    item_id: uuid.UUID
    output: dict[str, Any] = Field(default_factory=dict)
    scores: dict[str, float] = Field(default_factory=dict)
    trace_id: str | None = None


class RunIn(BaseModel):
    name: str
    config: dict[str, Any] = Field(default_factory=dict)
    items: list[RunItemIn]


@router.post("/v1/datasets/{name}/runs", status_code=201)
async def create_run(name: str, body: RunIn, p: Write) -> dict[str, Any]:
    """An offline experiment: one run = one config (prompt version/model) over the dataset.
    The summary (mean of each score) is what the promotion gate and comparisons use."""
    keys = {k for it in body.items for k in it.scores}
    summary = {k: sum(it.scores.get(k, 0.0) for it in body.items if k in it.scores) /
               max(1, sum(1 for it in body.items if k in it.scores)) for k in keys}
    async with session() as s:
        ds = await _dataset(s, p.project_id, name)
        run = DatasetRun(id=uuid.uuid4(), dataset_id=ds.id, name=body.name, config=body.config,
                         summary={"means": summary, "items": len(body.items)})
        s.add(run)
        await s.flush()
        s.add_all([DatasetRunItem(run_id=run.id, item_id=it.item_id, trace_id=it.trace_id, output=it.output,
                                  scores=it.scores) for it in body.items])
    return {"run_id": str(run.id), "summary": summary}


@router.get("/v1/datasets/{name}/runs")
async def list_runs(name: str, p: Read) -> list[dict[str, Any]]:
    async with session() as s:
        ds = await _dataset(s, p.project_id, name)
        rows = (await s.execute(select(DatasetRun).where(DatasetRun.dataset_id == ds.id)
                                .order_by(DatasetRun.created_at.desc()))).scalars()
        return [{"run_id": str(r.id), "name": r.name, "config": r.config, "summary": r.summary,
                 "created_at": r.created_at.isoformat()} for r in rows]


# ----------------------------------------------------------------------------- annotation queues
class QueueIn(BaseModel):
    name: str
    score_names: list[str] = Field(default_factory=lambda: ["correctness"])


@router.post("/v1/annotation-queues", status_code=201)
async def create_queue(body: QueueIn, p: Write) -> dict[str, str]:
    async with session() as s:
        q = AnnotationQueue(id=uuid.uuid4(), project_id=p.project_id, name=body.name, score_names=body.score_names)
        s.add(q)
    return {"queue_id": str(q.id)}


@router.post("/v1/annotation-queues/{queue_id}/items", status_code=201)
async def enqueue_items(queue_id: uuid.UUID, trace_ids: list[str], p: Write, reason: str = "") -> dict[str, int]:
    async with session() as s:
        q = await s.get(AnnotationQueue, queue_id)
        if q is None or q.project_id != p.project_id:
            raise NotFound("queue not found")
        s.add_all([AnnotationItem(queue_id=queue_id, trace_id=t.lower(), reason=reason) for t in trace_ids])
    return {"added": len(trace_ids)}


@router.post("/v1/annotation-queues/{queue_id}/claim")
async def claim_next(queue_id: uuid.UUID, p: Write, annotator: str) -> dict[str, Any]:
    """Claims the next item with a 15-minute lock (FOR UPDATE SKIP LOCKED: annotators never
    collide, abandoned locks expire)."""
    now = datetime.now(UTC)
    async with session() as s:
        q = await s.get(AnnotationQueue, queue_id)
        if q is None or q.project_id != p.project_id:
            raise NotFound("queue not found")
        item = (await s.execute(select(AnnotationItem).where(
            AnnotationItem.queue_id == queue_id,
            or_(AnnotationItem.status == "pending",
                and_(AnnotationItem.status == "locked", AnnotationItem.locked_until < now)))
            .order_by(AnnotationItem.created_at).limit(1).with_for_update(skip_locked=True))).scalar_one_or_none()
        if item is None:
            raise NotFound("queue is empty")
        item.status, item.locked_by, item.locked_until = "locked", annotator, now + timedelta(minutes=15)
        return {"item_id": str(item.id), "trace_id": item.trace_id, "reason": item.reason,
                "score_names": q.score_names}


class LabelIn(BaseModel):
    scores: dict[str, float]
    comment: str = ""


@router.post("/v1/annotation-items/{item_id}/complete")
async def complete_item(item_id: uuid.UUID, body: LabelIn, request: Request, p: Write) -> dict[str, str]:
    async with session() as s:
        item = await s.get(AnnotationItem, item_id)
        if item is None:
            raise NotFound("item not found")
        if item.status == "done":
            raise Conflict("already annotated")
        item.status = "done"
        trace_id, annotator = item.trace_id, item.locked_by or p.key_id
    scores = [ScoreIn(trace_id=trace_id, name=n, value=v, comment=body.comment, source="annotation",
                      score_id=f"ann:{item_id}:{n}") for n, v in body.scores.items()]
    await write_scores(request.app.state.ch, p.project_id, scores, annotator)
    return {"status": "done"}


# ----------------------------------------------------------------------------- embedding migrations
class MigrationIn(BaseModel):
    from_model: str
    to_model: str
    source_uri: str = Field(description="s3://<bucket>/<prefix> of JSONL files with {id, text}")
    target_uri: str = Field(description="s3://<bucket>/<prefix> to write {id, vector, model} JSONL")


@router.post("/v1/embedding-migrations", status_code=202)
async def start_migration(body: MigrationIn, request: Request, p: Admin) -> dict[str, str]:
    async with session() as s:
        job = ReembedJob(id=uuid.uuid4(), from_model=body.from_model, to_model=body.to_model,
                         source_uri=body.source_uri, target_uri=body.target_uri)
        s.add(job)
    await request.app.state.arq.enqueue_job("reembed", str(job.id), _queue_name="lumen:jobs",
                                            _job_id=f"reembed:{job.id}")
    return {"job_id": str(job.id), "status": "queued"}


@router.get("/v1/embedding-migrations/{job_id}")
async def migration_status(job_id: uuid.UUID, p: Read) -> dict[str, Any]:
    async with session() as s:
        job = await s.get(ReembedJob, job_id)
    if job is None:
        raise NotFound("job not found")
    return {"job_id": str(job.id), "status": job.status, "progress": job.progress, "to_model": job.to_model}


# ----------------------------------------------------------------------------- alerts
class RuleIn(BaseModel):
    name: str
    kind: Literal["slo_errors", "slo_latency", "cost_anomaly", "quality_drop", "cache_drop", "drift"]
    params: dict[str, Any] = Field(default_factory=dict,
                                   examples=[{"target": 0.99, "route": "answer"}, {"score": "groundedness"}])
    webhook_url: str | None = None


@router.post("/v1/alerts/rules", status_code=201)
async def create_rule(body: RuleIn, p: Write) -> dict[str, str]:
    async with session() as s:
        r = AlertRule(id=uuid.uuid4(), project_id=p.project_id, name=body.name, kind=body.kind, params=body.params,
                      webhook_url=body.webhook_url)
        s.add(r)
    return {"rule_id": str(r.id)}


@router.get("/v1/alerts/rules")
async def list_rules(p: Read) -> list[dict[str, Any]]:
    async with session() as s:
        rows = (await s.execute(select(AlertRule).where(AlertRule.project_id == p.project_id))).scalars()
        return [{"id": str(r.id), "name": r.name, "kind": r.kind, "params": r.params, "enabled": r.enabled}
                for r in rows]


@router.delete("/v1/alerts/rules/{rule_id}", status_code=204)
async def disable_rule(rule_id: uuid.UUID, p: Write) -> None:
    async with session() as s:
        await s.execute(update(AlertRule).where(AlertRule.id == rule_id, AlertRule.project_id == p.project_id)
                        .values(enabled=False))


@router.get("/v1/alerts/events")
async def list_events(p: Read, limit: int = 100) -> list[dict[str, Any]]:
    async with session() as s:
        rows = (await s.execute(select(AlertEvent, AlertRule.name).join(AlertRule, AlertRule.id == AlertEvent.rule_id)
                                .where(AlertRule.project_id == p.project_id)
                                .order_by(AlertEvent.created_at.desc()).limit(min(limit, 1000)))).all()
        return [{"rule": n, "state": e.state, "value": e.value, "message": e.message,
                 "at": e.created_at.isoformat()} for e, n in rows]
