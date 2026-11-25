"""arq worker: online evaluators, alert evaluation, experiment auto-stop, drift, re-embedding,
raw-batch retention.   Run:  arq lumen.workers.main.WorkerSettings

(The OTLP ingest consumer is a separate process, ``python -m lumen.ingest.consumer``, because it
is a long-lived stream consumer rather than a job queue.)
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from arq import cron
from arq.connections import RedisSettings
from sqlalchemy import select

from lumen.alerts import detectors as D
from lumen.alerts.engine import evaluate_rules, record
from lumen.common.logging import configure_logging, get_logger
from lumen.common.settings import get_settings
from lumen.common.telemetry import setup_telemetry
from lumen.evals.evaluators import GroundednessJudge, evaluate_cheap, split_context
from lumen.experiments.service import analyze, auto_stop_decision, set_status
from lumen.ingest.consumer import EVAL_QUEUE
from lumen.storage.clickhouse import SCORE_COLUMNS, ClickHouse, connect
from lumen.storage.models import AlertRule, AnnotationItem, AnnotationQueue, Experiment, ReembedJob
from lumen.storage.objects import ObjectStore
from lumen.storage.postgres import session

log = get_logger(__name__)
LOW_GROUNDEDNESS = 0.5


async def startup(ctx: dict[str, Any]) -> None:
    s = get_settings()
    configure_logging(s.log_level)
    setup_telemetry(f"{s.service_name}-worker", s.otlp_endpoint)
    ctx["s"] = s
    ctx["ch"] = ClickHouse(await connect(s.clickhouse_url))
    ctx["objects"] = ObjectStore(s)
    ctx["judge"] = GroundednessJudge(s.judge_url, s.judge_api_key.get_secret_value(), s.judge_route)


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["judge"].aclose()
    await ctx["ch"].close()


# ----------------------------------------------------------------------------- online evals
async def run_online_evals(ctx: dict[str, Any], max_items: int = 200) -> int:
    redis = ctx["redis"]
    raw = await redis.lpop(EVAL_QUEUE, max_items)
    if not raw:
        return 0
    rows, low = [], []
    for blob in raw:
        it = json.loads(blob)
        scores = evaluate_cheap(it.get("output_preview", ""), it.get("finish_reason", ""))
        context, _ = split_context(it.get("input_preview", ""))
        if context and it.get("output_preview"):
            g = await ctx["judge"].grade(context, it["output_preview"])
            if g:
                scores.append(g)
                if g.value < LOW_GROUNDEDNESS:
                    low.append((it["project_id"], it["trace_id"], g.value))
        for sc in scores:
            rows.append({"project_id": it["project_id"], "score_id": f"eval:{it['span_id']}:{sc.name}",
                         "trace_id": it["trace_id"], "span_id": it["span_id"], "session_id": it.get("session_id", ""),
                         "name": sc.name, "data_type": sc.data_type, "value": sc.value,
                         "string_value": sc.string_value, "source": "eval", "comment": sc.comment,
                         "author": "lumen-online-eval", "config_id": "",
                         "created_at": datetime.fromisoformat(it["end_time"])})
    if rows:
        await ctx["ch"].insert_rows("scores", SCORE_COLUMNS, rows, batch_id=uuid.uuid4().hex)
    if low:
        await _queue_for_review(low)
    return len(rows)


async def _queue_for_review(items: list[tuple[str, str, float]]) -> None:
    """Low-groundedness traces go to a human annotation queue: their labels both fix the data
    and calibrate the judge."""
    async with session() as s:
        for project_id, trace_id, score in items:
            pid = uuid.UUID(project_id)
            q = (await s.execute(select(AnnotationQueue).where(AnnotationQueue.project_id == pid,
                                                               AnnotationQueue.name == "low-groundedness"))
                 ).scalar_one_or_none()
            if q is None:
                q = AnnotationQueue(id=uuid.uuid4(), project_id=pid, name="low-groundedness",
                                    score_names=["groundedness_human", "correctness"])
                s.add(q)
                await s.flush()
            s.add(AnnotationItem(queue_id=q.id, trace_id=trace_id, reason=f"judge groundedness {score:.2f}"))


# ----------------------------------------------------------------------------- alerts & experiments
async def run_alerts(ctx: dict[str, Any]) -> int:
    async with session() as s:
        return await evaluate_rules(s, ctx["ch"], ctx["s"].alert_webhook_url, ctx["s"].public_url)


async def auto_stop_experiments(ctx: dict[str, Any]) -> int:
    stopped = 0
    async with session() as s:
        running = (await s.execute(select(Experiment).where(Experiment.status == "running"))).scalars().all()
        for exp in running:
            try:
                reason = auto_stop_decision(await analyze(ctx["ch"], exp))
            except Exception:
                log.exception("experiment_analysis_failed", key=exp.key)
                continue
            if reason:
                await set_status(s, exp.project_id, exp.key, "stopped", reason=f"auto-stop: {reason}")
                stopped += 1
                log.warning("experiment_auto_stopped", key=exp.key, reason=reason)
    return stopped


async def detect_drift(ctx: dict[str, Any]) -> int:
    """Embeds samples of recent vs reference user queries (needs content capture enabled)."""
    s = ctx["s"]
    fired = 0
    async with session() as db:
        rules = (await db.execute(select(AlertRule).where(AlertRule.kind == "drift", AlertRule.enabled.is_(True)))
                 ).scalars().all()
        for rule in rules:
            q = """SELECT input_preview FROM spans SAMPLE 0.5
                   WHERE project_id = {p:String} AND is_llm = 1 AND input_preview != ''
                     AND start_time BETWEEN {a:DateTime64(6)} AND {b:DateTime64(6)} LIMIT 300"""
            now = datetime.now(UTC).replace(tzinfo=None)
            cur = [r["input_preview"][-800:] for r in await ctx["ch"].query(
                q, {"p": str(rule.project_id), "a": now - timedelta(days=1), "b": now})]
            ref = [r["input_preview"][-800:] for r in await ctx["ch"].query(
                q, {"p": str(rule.project_id), "a": now - timedelta(days=8), "b": now - timedelta(days=1)})]
            if len(cur) < 20 or len(ref) < 20:
                continue
            embed_url = rule.params.get("embedder_url", "http://embedder:8080")
            async with httpx.AsyncClient(timeout=120) as client:
                vc = (await client.post(f"{embed_url}/v1/embed", json={"inputs": cur, "input_type": "query"})).json()
                vr = (await client.post(f"{embed_url}/v1/embed", json={"inputs": ref, "input_type": "query"})).json()
            det = D.query_drift(vc["embeddings"], vr["embeddings"], float(rule.params.get("threshold", 0.15)))
            fired += await record(db, rule, det, s.alert_webhook_url, s.public_url)
    return fired


# ----------------------------------------------------------------------------- re-embedding migrations
async def reembed(ctx: dict[str, Any], job_id: str, embedder_url: str = "http://embedder:8080",
                  batch: int = 256) -> dict[str, Any]:
    """Re-embeds JSONL files ({"id", "text"} per line) under source_uri with ``to_model`` and
    writes {"id", "vector", "model"} JSONL under target_uri. Resumable: progress.cursor records
    the last finished file, so a crashed job continues where it stopped (consumers then build a
    new index and swap aliases: blue/green, e.g. Atlas's Qdrant alias swap)."""
    objects: ObjectStore = ctx["objects"]
    async with session() as s:
        job = await s.get(ReembedJob, uuid.UUID(job_id))
        if job is None:
            return {"error": "not found"}
        job.status = "running"
        src, dst, model, done_files = job.source_uri, job.target_uri, job.to_model, set(job.progress.get("files", []))
    files = [k for k in await objects.list_prefix(src.split("/", 3)[-1]) if k.endswith(".jsonl")]
    total = 0
    async with httpx.AsyncClient(timeout=300) as client:
        for key in files:
            if key in done_files:
                continue
            lines = [json.loads(x) for x in ((await objects.get(key)) or b"").decode().splitlines() if x.strip()]
            out = []
            for i in range(0, len(lines), batch):
                part = lines[i:i + batch]
                r = await client.post(f"{embedder_url}/v1/embed", json={"inputs": [p["text"] for p in part],
                                                                       "model": model, "input_type": "document"})
                r.raise_for_status()
                out.extend({"id": p["id"], "vector": v, "model": r.json()["model"]}
                           for p, v in zip(part, r.json()["embeddings"], strict=True))
            target = f"{dst.split('/', 3)[-1].rstrip('/')}/{key.rsplit('/', 1)[-1]}"
            await objects.put(target, "\n".join(json.dumps(o) for o in out).encode(), "application/x-ndjson")
            total += len(out)
            done_files.add(key)
            async with session() as s:
                job = await s.get(ReembedJob, uuid.UUID(job_id))
                assert job is not None
                job.progress = {"files": sorted(done_files), "done": len(done_files), "total": len(files)}
    async with session() as s:
        job = await s.get(ReembedJob, uuid.UUID(job_id))
        assert job is not None
        job.status = "succeeded"
    return {"files": len(files), "vectors": total}


async def cleanup_raw_batches(ctx: dict[str, Any], keep_days: int = 7) -> int:
    """Raw OTLP batches are only needed for replay; ClickHouse holds the data. (In S3 proper,
    prefer a bucket lifecycle rule; this job covers MinIO dev setups.)"""
    objects: ObjectStore = ctx["objects"]
    cutoff = datetime.now(UTC) - timedelta(days=keep_days)
    removed = 0
    for key in await objects.list_prefix("otlp/"):
        parts = key.split("/")  # otlp/<project>/YYYY/MM/DD/HH/<batch>
        try:
            ts = datetime(int(parts[2]), int(parts[3]), int(parts[4]), int(parts[5]), tzinfo=UTC)
        except (IndexError, ValueError):
            continue
        if ts < cutoff:
            await objects.delete(key)
            removed += 1
    return removed


class WorkerSettings:
    functions = [reembed]
    cron_jobs = [
        cron(run_online_evals, second={0, 15, 30, 45}),
        cron(run_alerts, second=5),
        cron(auto_stop_experiments, minute={0, 15, 30, 45}, second=20),
        cron(detect_drift, hour=4, minute=0),
        cron(cleanup_raw_batches, hour=3, minute=30),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    queue_name = "lumen:jobs"
    max_jobs = 8
    job_timeout = 3600
