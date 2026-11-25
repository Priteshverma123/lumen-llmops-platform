"""Experiment lifecycle + analysis.

Producers tag spans with ``lumen.experiment.id`` and ``lumen.experiment.variant`` (the SDK does it
when it serves a prompt through an experiment). Analysis reads ClickHouse:
  * unit counts per variant → SRM check (a mismatch invalidates the readout);
  * primary metric = mean of a score (e.g. online ``groundedness`` or user ``feedback``) per trace;
  * guardrails = error rate, cost per call, p95 latency per variant;
  * fixed-horizon Welch test + always-valid sequential CI (the one used for auto-stop).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen.common.errors import Conflict, InvalidInput, NotFound
from lumen.experiments.assignment import Variant, assign
from lumen.experiments.stats import check_guardrails, sequential_ci, srm, welch
from lumen.storage.clickhouse import ClickHouse
from lumen.storage.models import Experiment

UNIT_COLUMN = {"user": "user_id", "session": "session_id", "tenant": "tenant"}


def variants_of(exp: Experiment) -> list[Variant]:
    return [Variant(v["name"], float(v.get("weight", 1))) for v in exp.variants["variants"]]


async def get_experiment(s: AsyncSession, project_id: uuid.UUID, key: str) -> Experiment:
    exp = (await s.execute(select(Experiment).where(Experiment.project_id == project_id, Experiment.key == key))
           ).scalar_one_or_none()
    if exp is None:
        raise NotFound(f"experiment '{key}' not found")
    return exp


async def assign_unit(s: AsyncSession, project_id: uuid.UUID, key: str, unit_id: str) -> dict[str, Any]:
    exp = await get_experiment(s, project_id, key)
    if exp.status != "running":
        control = exp.variants["variants"][0]
        return {"experiment": key, "variant": control["name"], "config": control, "active": False}
    name = assign(exp.key, unit_id, variants_of(exp))
    cfg = next(v for v in exp.variants["variants"] if v["name"] == name)
    return {"experiment": key, "variant": name, "config": cfg, "active": True}


async def set_status(s: AsyncSession, project_id: uuid.UUID, key: str, status: str, reason: str = "") -> Experiment:
    exp = await get_experiment(s, project_id, key)
    allowed = {"draft": {"running"}, "running": {"stopped", "completed"}, "stopped": {"running", "completed"}}
    if status not in allowed.get(exp.status, set()):
        raise Conflict(f"cannot move experiment from {exp.status} to {status}")
    exp.status = status
    if status == "running" and exp.started_at is None:
        exp.started_at = datetime.now(UTC)
    if reason:
        exp.stop_reason = reason
    return exp


async def analyze(ch: ClickHouse, exp: Experiment, alpha: float = 0.05) -> dict[str, Any]:
    if exp.started_at is None:
        raise InvalidInput("experiment has not started")
    unit_col = UNIT_COLUMN.get(exp.unit, "user_id")
    params = {"p": str(exp.project_id), "e": exp.key, "since": exp.started_at.replace(tzinfo=None)}
    counts = {r["variant"]: r for r in await ch.query(f"""
        SELECT variant, uniqExact({unit_col}) AS units, count() AS calls, countIf(status = 'error') / count() AS error_rate,
               avg(cost_usd) AS cost_per_call, quantile(0.95)(duration_ms) AS p95_latency_ms
        FROM spans WHERE project_id = {{p:String}} AND experiment_id = {{e:String}} AND start_time >= {{since:DateTime64(6)}}
          AND is_llm = 1
        GROUP BY variant""", params)}
    metric_name = exp.primary_metric.get("score", "feedback")
    metric = {r["variant"]: r for r in await ch.query("""
        SELECT s.variant AS variant, count() AS n, avg(sc.value) AS mean, varSamp(sc.value) AS var
        FROM scores AS sc
        INNER JOIN (SELECT DISTINCT trace_id, variant FROM spans
                    WHERE project_id = {p:String} AND experiment_id = {e:String}
                      AND start_time >= {since:DateTime64(6)}) AS s ON s.trace_id = sc.trace_id
        WHERE sc.project_id = {p:String} AND sc.name = {m:String}
        GROUP BY variant""", params | {"m": metric_name})}

    vs = variants_of(exp)
    control = vs[0].name
    srm_res = srm([int(counts.get(v.name, {}).get("units", 0)) for v in vs], [v.weight for v in vs])
    out: dict[str, Any] = {"experiment": exp.key, "status": exp.status, "metric": metric_name,
                           "srm": {"chi2": srm_res.chi2, "p_value": srm_res.p_value, "mismatch": srm_res.mismatch},
                           "variants": {}, "comparisons": {}, "guardrail_breaches": {}}
    for v in vs:
        out["variants"][v.name] = {**{k: float(x) for k, x in counts.get(v.name, {}).items() if k != "variant"},
                                   **{f"metric_{k}": float(x) for k, x in metric.get(v.name, {}).items()
                                      if k != "variant" and x is not None}}
    c = metric.get(control)
    for v in vs[1:]:
        t = metric.get(v.name)
        if c and t and c["n"] > 1 and t["n"] > 1:
            fixed = welch(c["mean"], c["var"], c["n"], t["mean"], t["var"], t["n"], alpha)
            seq = sequential_ci(c["mean"], c["var"], c["n"], t["mean"], t["var"], t["n"], alpha)
            out["comparisons"][v.name] = {"fixed_horizon": _asdict(fixed), "sequential": _asdict(seq)}
        breaches = check_guardrails(_metrics(counts.get(control)), _metrics(counts.get(v.name)), exp.guardrails or {})
        if breaches:
            out["guardrail_breaches"][v.name] = [_asdict(b) for b in breaches]
    return out


def _metrics(row: dict[str, Any] | None) -> dict[str, float]:
    row = row or {}
    return {k: float(row.get(k, 0) or 0) for k in ("error_rate", "cost_per_call", "p95_latency_ms")}


def _asdict(obj: Any) -> dict[str, Any]:
    return {f: getattr(obj, f) for f in obj.__slots__}


def auto_stop_decision(analysis: dict[str, Any]) -> str | None:
    """Stop when: SRM detected (broken experiment), any guardrail breached, or the sequential
    CI shows a significant *harm* on the primary metric. Wins are left for a human to ship."""
    if analysis["srm"]["mismatch"]:
        return f"sample ratio mismatch (p={analysis['srm']['p_value']:.2g})"
    if analysis["guardrail_breaches"]:
        return f"guardrail breach: {analysis['guardrail_breaches']}"
    for name, comp in analysis["comparisons"].items():
        seq = comp["sequential"]
        if seq["significant"] and seq["ci_high"] < 0:
            return f"variant {name} significantly worse on {analysis['metric']}"
    return None
