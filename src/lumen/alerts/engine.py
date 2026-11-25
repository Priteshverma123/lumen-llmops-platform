"""Alert rule evaluation (cron) + notification on state transitions only (no repeat spam)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen.alerts import detectors as D
from lumen.common.logging import get_logger
from lumen.storage.clickhouse import ClickHouse
from lumen.storage.models import AlertEvent, AlertRule

log = get_logger(__name__)
WINDOWS = {"5m": 5, "30m": 30, "1h": 60, "6h": 360}


async def _slo(ch: ClickHouse, rule: AlertRule) -> D.Detection:
    p = rule.params
    bad_expr = "status = 'error'" if rule.kind == "slo_errors" else "duration_ms > {thr:Float64}"
    windows = {}
    for name, minutes in WINDOWS.items():
        rows = await ch.query(f"""SELECT countIf({bad_expr}) AS bad, count() AS total FROM spans
            WHERE project_id = {{p:String}} AND is_llm = 1 AND start_time > now64() - INTERVAL {minutes} MINUTE
              AND ({{route:String}} = '' OR route = {{route:String}})""",
                              {"p": str(rule.project_id), "route": p.get("route", ""),
                               "thr": float(p.get("latency_ms", 5000))})
        windows[name] = (float(rows[0]["bad"]), float(rows[0]["total"]))
    return D.multiwindow_burn(windows, float(p.get("target", 0.99)))


async def _cost(ch: ClickHouse, rule: AlertRule) -> D.Detection:
    rows = await ch.query("""SELECT toStartOfHour(minute) AS h, sum(cost_usd) AS cost FROM llm_minute
        WHERE project_id = {p:String} AND minute > now() - INTERVAL 29 DAY GROUP BY h ORDER BY h""",
                          {"p": str(rule.project_id)})
    series = {r["h"]: float(r["cost"]) for r in rows}
    now_h = datetime.now(UTC).replace(minute=0, second=0, microsecond=0, tzinfo=None) - timedelta(hours=1)
    current = series.get(now_h, 0.0)  # last *complete* hour
    weekly = [series.get(now_h - timedelta(weeks=w), 0.0) for w in (1, 2, 3, 4) if now_h - timedelta(weeks=w) in series]
    trailing = [series.get(now_h - timedelta(hours=h), 0.0) for h in range(1, 25)]
    return D.cost_anomaly(current, weekly, trailing, float(rule.params.get("z", 3.5)),
                          float(rule.params.get("min_usd", 1.0)))


async def _quality(ch: ClickHouse, rule: AlertRule) -> D.Detection:
    q = """SELECT avgIf(value, created_at > now64() - INTERVAL 1 DAY) AS cur,
                  countIf(created_at > now64() - INTERVAL 1 DAY) AS n,
                  avgIf(value, created_at <= now64() - INTERVAL 1 DAY) AS base
           FROM scores WHERE project_id = {p:String} AND name = {s:String} AND created_at > now64() - INTERVAL 8 DAY"""
    r = (await ch.query(q, {"p": str(rule.project_id), "s": rule.params.get("score", "groundedness")}))[0]
    return D.quality_drop(float(r["cur"] or 0), float(r["base"] or 0), int(r["n"] or 0),
                          float(rule.params.get("max_drop", 0.05)))


async def _cache(ch: ClickHouse, rule: AlertRule) -> D.Detection:
    q = """SELECT sumIf(cache_read_tokens, minute > now() - INTERVAL 1 HOUR) /
                  greatest(1, sumIf(input_tokens + cache_read_tokens, minute > now() - INTERVAL 1 HOUR)) AS cur,
                  sumIf(cache_read_tokens, minute <= now() - INTERVAL 1 HOUR) /
                  greatest(1, sumIf(input_tokens + cache_read_tokens, minute <= now() - INTERVAL 1 HOUR)) AS base
           FROM llm_minute WHERE project_id = {p:String} AND minute > now() - INTERVAL 1 DAY"""
    r = (await ch.query(q, {"p": str(rule.project_id)}))[0]
    return D.cache_hit_drop(float(r["cur"] or 0), float(r["base"] or 0))


DETECTORS: dict[str, Any] = {"slo_errors": _slo, "slo_latency": _slo, "cost_anomaly": _cost,
                             "quality_drop": _quality, "cache_drop": _cache}


async def notify(url: str, rule: AlertRule, det: D.Detection, public_url: str) -> None:
    state = "FIRING" if det.firing else "RESOLVED"
    text = f"[{state}] {rule.name} ({rule.kind}): {det.message}\n{public_url}/alerts/{rule.id}"
    # Slack incoming webhooks accept {"text": ...}; generic receivers get the structured fields too.
    payload = {"text": text, "rule": rule.name, "kind": rule.kind, "state": state.lower(), "value": det.value}
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            (await client.post(url, json=payload)).raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("alert_notify_failed", rule=rule.name, error=str(exc))


async def evaluate_rules(s: AsyncSession, ch: ClickHouse, default_webhook: str | None, public_url: str) -> int:
    rules = (await s.execute(select(AlertRule).where(AlertRule.enabled.is_(True)))).scalars().all()
    transitions = 0
    for rule in rules:
        fn = DETECTORS.get(rule.kind)
        if fn is None:
            continue  # 'drift' runs in its own daily job (needs embeddings)
        try:
            det = await fn(ch, rule)
        except Exception:
            log.exception("alert_eval_failed", rule=rule.name)
            continue
        transitions += await record(s, rule, det, default_webhook, public_url)
    return transitions


async def record(s: AsyncSession, rule: AlertRule, det: D.Detection, default_webhook: str | None,
                 public_url: str) -> int:
    last = (await s.execute(select(AlertEvent).where(AlertEvent.rule_id == rule.id)
                            .order_by(AlertEvent.created_at.desc()).limit(1))).scalar_one_or_none()
    was_firing = last is not None and last.state == "firing"
    if det.firing == was_firing:
        return 0
    s.add(AlertEvent(rule_id=rule.id, state="firing" if det.firing else "resolved", value=det.value,
                     message=det.message))
    if url := (rule.webhook_url or default_webhook):
        await notify(url, rule, det, public_url)
    return 1
