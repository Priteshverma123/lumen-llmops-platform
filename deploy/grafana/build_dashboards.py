"""Dashboards-as-code: generates Grafana dashboard JSON from compact panel specs.

    python deploy/grafana/build_dashboards.py      # writes deploy/grafana/dashboards/*.json

Queries read the rollup tables (llm_minute / llm_hour) so panels stay fast over long ranges;
trace-level panels read ``spans`` with a time filter. ``$__timeFilter`` and ``$__timeInterval``
are macros of the official ClickHouse datasource plugin.
"""

from __future__ import annotations

import json
from pathlib import Path

DS = {"type": "grafana-clickhouse-datasource", "uid": "lumen-clickhouse"}
OUT = Path(__file__).parent / "dashboards"


def panel(pid: int, title: str, sql: str, *, kind: str = "timeseries", unit: str = "short", x: int = 0,
          y: int = 0, w: int = 12, h: int = 8) -> dict:
    return {"id": pid, "type": kind, "title": title, "datasource": DS,
            "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
            "targets": [{"refId": "A", "datasource": DS, "editorType": "sql", "rawSql": sql.strip(),
                         "format": 0 if kind == "timeseries" else 1, "queryType": "timeseries"
                         if kind == "timeseries" else "table"}]}


def dashboard(uid: str, title: str, panels: list[dict]) -> dict:
    return {"uid": uid, "title": title, "tags": ["lumen", "llm"], "timezone": "utc", "schemaVersion": 39,
            "time": {"from": "now-24h", "to": "now"}, "refresh": "1m", "panels": panels,
            "templating": {"list": [{"name": "project", "type": "query", "datasource": DS,
                                     "query": "SELECT DISTINCT project_id FROM llm_hour", "refresh": 2}]}}


P = "project_id = '${project}'"
ROLL = f"FROM llm_minute WHERE {P} AND $__timeFilter(minute)"

overview = dashboard("lumen-overview", "Lumen: LLM overview", [
    panel(1, "Requests / min", f"SELECT minute AS time, sum(calls) AS calls {ROLL} GROUP BY time ORDER BY time"),
    panel(2, "Error rate", f"SELECT minute AS time, sum(errors)/sum(calls) AS error_rate {ROLL} GROUP BY time "
                           "ORDER BY time", unit="percentunit", x=12),
    panel(3, "Latency p50/p95/p99 (ms)", f"""SELECT minute AS time,
        quantilesTDigestMerge(0.5,0.95,0.99)(latency)[1] AS p50, quantilesTDigestMerge(0.5,0.95,0.99)(latency)[2] AS p95,
        quantilesTDigestMerge(0.5,0.95,0.99)(latency)[3] AS p99 {ROLL} GROUP BY time ORDER BY time""", unit="ms", y=8),
    panel(4, "TTFT p50/p95 (ms)", f"""SELECT minute AS time, quantilesTDigestMerge(0.5,0.95)(ttft)[1] AS p50,
        quantilesTDigestMerge(0.5,0.95)(ttft)[2] AS p95 {ROLL} GROUP BY time ORDER BY time""", unit="ms", x=12, y=8),
    panel(5, "Tokens / min", f"""SELECT minute AS time, sum(input_tokens) AS input, sum(output_tokens) AS output,
        sum(cache_read_tokens) AS cache_read {ROLL} GROUP BY time ORDER BY time""", y=16),
    panel(6, "Provider prompt-cache read ratio", f"""SELECT minute AS time,
        sum(cache_read_tokens)/greatest(1, sum(input_tokens)+sum(cache_read_tokens)) AS ratio {ROLL}
        GROUP BY time ORDER BY time""", unit="percentunit", x=12, y=16),
])

cost = dashboard("lumen-cost", "Lumen: cost attribution", [
    panel(1, "Spend per hour by route", f"""SELECT toStartOfHour(minute) AS time, route, sum(cost_usd) AS usd {ROLL}
        GROUP BY time, route ORDER BY time""", unit="currencyUSD", w=24),
    panel(2, "Spend by tenant (range)", f"""SELECT tenant, sum(cost_usd) AS usd, sum(calls) AS calls,
        sum(cost_usd)/sum(calls) AS usd_per_call {ROLL} GROUP BY tenant ORDER BY usd DESC LIMIT 50""",
          kind="table", y=8),
    panel(3, "Spend by model (range)", f"""SELECT provider, model, sum(cost_usd) AS usd, sum(input_tokens) AS input,
        sum(output_tokens) AS output {ROLL} GROUP BY provider, model ORDER BY usd DESC""", kind="table", x=12, y=8),
    panel(4, "Cost per call by prompt version", f"""SELECT minute AS time, concat(prompt_name,'@v',
        toString(prompt_version)) AS prompt, sum(cost_usd)/sum(calls) AS usd_per_call {ROLL} AND prompt_name != ''
        GROUP BY time, prompt ORDER BY time""", unit="currencyUSD", y=16, w=24),
])

quality = dashboard("lumen-quality", "Lumen: quality & evals", [
    panel(1, "Online groundedness (hourly mean)", f"""SELECT toStartOfHour(created_at) AS time, avg(value) AS
        groundedness FROM scores WHERE {P} AND name = 'groundedness' AND $__timeFilter(created_at)
        GROUP BY time ORDER BY time""", unit="percentunit"),
    panel(2, "User feedback (share positive)", f"""SELECT toStartOfHour(created_at) AS time, avg(value) AS positive
        FROM scores WHERE {P} AND name = 'feedback' AND $__timeFilter(created_at) GROUP BY time ORDER BY time""",
          unit="percentunit", x=12),
    panel(3, "Refusal / PII / truncation rates", f"""SELECT toStartOfHour(created_at) AS time,
        avgIf(value, name='refusal') AS refusal, avgIf(value, name='pii_leak') AS pii, avgIf(value, name='truncated')
        AS truncated FROM scores WHERE {P} AND source = 'eval' AND $__timeFilter(created_at) GROUP BY time
        ORDER BY time""", unit="percentunit", y=8),
    panel(4, "Groundedness by prompt version", f"""SELECT concat(s.prompt_name,'@v',toString(s.prompt_version)) AS
        prompt, avg(sc.value) AS groundedness, count() AS n FROM scores AS sc INNER JOIN (SELECT DISTINCT trace_id,
        prompt_name, prompt_version FROM spans WHERE {P} AND $__timeFilter(start_time) AND prompt_name != '') AS s
        ON s.trace_id = sc.trace_id WHERE sc.name = 'groundedness' GROUP BY prompt ORDER BY prompt""",
          kind="table", x=12, y=8),
])

embeddings = dashboard("lumen-embeddings", "Lumen: embedding service", [
    panel(1, "Items / min by model", f"""SELECT toStartOfMinute(ts) AS time, model, sum(items) AS items
        FROM embedding_requests WHERE $__timeFilter(ts) GROUP BY time, model ORDER BY time"""),
    panel(2, "Cache hit rate", """SELECT toStartOfMinute(ts) AS time, sum(cache_hits)/greatest(1,sum(items)) AS
        hit_rate FROM embedding_requests WHERE $__timeFilter(ts) GROUP BY time ORDER BY time""",
          unit="percentunit", x=12),
    panel(3, "Request latency p95 (ms)", """SELECT toStartOfMinute(ts) AS time, quantile(0.95)(compute_ms) AS p95
        FROM embedding_requests WHERE $__timeFilter(ts) GROUP BY time ORDER BY time""", unit="ms", y=8, w=24),
])

if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for d in (overview, cost, quality, embeddings):
        (OUT / f"{d['uid']}.json").write_text(json.dumps(d, indent=2), encoding="utf-8")
        print("wrote", d["uid"])
