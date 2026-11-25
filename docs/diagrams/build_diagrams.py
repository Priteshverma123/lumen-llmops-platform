"""Generates docs/diagrams/lumen.drawio (diagrams-as-code).   python docs/diagrams/build_diagrams.py"""

from pathlib import Path

from _drawio import Document

doc = Document()

# ---------------------------------------------------------------------------- 1. system architecture
p = doc.page("1. System Architecture")
p.title("Lumen: system architecture (containers)",
        "Solid = sync request, dashed = async/queue, red = data write. Every box is a compose service.")
p.node("apps", "Atlas · Relay · any app<br><i>OTel SDK (gen_ai.*)</i>", 30, 120, 170, 60, kind="client")
p.node("sdk", "lumen-sdk<br><i>prompts · assign · tags</i>", 30, 230, 170, 60, kind="client")
p.node("ui", "Grafana / API clients", 30, 340, 170, 50, kind="client")
p.node("col", "otel-collector<br><i>tail sampling · redaction</i>", 250, 110, 180, 70, kind="service")
edge = p.group("edge", "lumen-api (FastAPI)", 480, 90, 300, 330, kind="zone")
p.node("ing", "OTLP ingest /v1/traces", 20, 40, 260, 44, kind="component", parent=edge)
p.node("q", "Query API: traces · sessions · metrics", 20, 95, 260, 44, kind="component", parent=edge)
p.node("reg", "Prompt registry + experiments", 20, 150, 260, 44, kind="component", parent=edge)
p.node("ds", "Scores · datasets · annotation queues", 20, 205, 260, 44, kind="component", parent=edge)
p.node("al", "Alert rules · embedding migrations", 20, 260, 260, 44, kind="component", parent=edge)
wk = p.group("wk", "Workers", 830, 90, 260, 330, kind="zone")
p.node("ic", "ingest consumer<br><i>stream group · normalise · cost</i>", 20, 40, 220, 60, kind="worker", parent=wk)
p.node("ev", "arq worker<br><i>online evals · alerts · auto-stop<br>drift · re-embed · retention</i>", 20, 120,
       220, 80, kind="worker", parent=wk)
p.node("emb", "lumen-embedder<br><i>cache · single-flight · micro-batch</i>", 20, 230, 220, 70, kind="service",
       parent=wk)
data = p.group("data", "Data plane", 250, 470, 840, 130, kind="zone")
p.node("ch", "ClickHouse<br>spans · scores · rollups", 20, 35, 180, 80, kind="db", parent=data)
p.node("pg", "Postgres<br>registry · experiments · datasets", 220, 35, 200, 80, kind="db", parent=data)
p.node("rd", "Redis<br>stream · queues · caches", 440, 35, 180, 80, kind="cache", parent=data)
p.node("s3", "MinIO / S3<br>raw OTLP batches", 640, 35, 180, 80, kind="storage", parent=data)
p.node("tei", "TEI embed / rerank<br>(cpu | gpu profile)", 1140, 330, 170, 60, kind="llm")
p.node("judge", "Judge LLM<br>(Atlas gateway route 'judge')", 1140, 200, 170, 60, kind="external")
p.node("hook", "Slack / webhook", 1140, 100, 150, 60, kind="external")
p.edge("apps", "col", "OTLP")
p.edge("col", "ing", "OTLP/HTTP")
p.edge("sdk", "reg", "GET prompt (ETag)")
p.edge("ui", "q")
p.edge("ing", "s3", "1. raw batch", kind="data")
p.edge("ing", "rd", "2. XADD ref", kind="async")
p.edge("rd", "ic", "XREADGROUP", kind="async")
p.edge("ic", "ch", "batched insert", kind="data")
p.edge("ev", "judge")
p.edge("ev", "hook")
p.edge("emb", "tei")
p.edge("reg", "pg", kind="data")
p.edge("q", "ch")
p.legend(1140, 440)

# ---------------------------------------------------------------------------- 2. ingest path
p = doc.page("2. OTLP Ingest Path")
p.title("Ingest: S3-first durability → Redis stream → worker → ClickHouse",
        "Ack only after the raw batch is durable. At-least-once delivery + insert dedup tokens = exactly-once effect.")
steps = [("rx", "POST /v1/traces<br>(protobuf | JSON, gzip)", "start"),
         ("auth", "auth: project key (scope ingest)<br>size limit · content-type", "step"),
         ("bp", "stream backlog > 50k?", "decision"),
         ("put", "PUT raw batch → MinIO<br>otlp/&lt;project&gt;/YYYY/MM/DD/HH/&lt;batch&gt;", "step"),
         ("xadd", "XADD lumen:ingest {batch_id, key}", "step"),
         ("ok", "200 OK (ExportTraceServiceResponse)", "end")]
y = 90
for k, lbl, kind in steps:
    p.node(k, lbl, 60, y, 280, 80 if kind == "decision" else 54, kind=kind)
    y += 110 if kind == "decision" else 85
for a, b in zip([s[0] for s in steps], [s[0] for s in steps][1:]):
    p.edge(a, b, "no" if a == "bp" else "", kind="flow")
p.node("429", "429 + Retry-After<br>(OTLP exporters retry)", 400, 270, 200, 54, kind="end")
p.edge("bp", "429", "yes", kind="flow")
w = [("claim", "XAUTOCLAIM idle &gt; 60s<br>(recover crashed consumers)"),
     ("read", "XREADGROUP count=50, block 2s"),
     ("get", "GET raw batches from MinIO"),
     ("dec", "decode protobuf/JSON → RawSpan"),
     ("map", "semconv mapping (new + legacy + OpenInference)<br>disjoint token buckets"),
     ("cost", "cost from versioned price manifest<br>(client cost wins)"),
     ("ins", "INSERT spans (+ eval-event scores)<br>dedup token = hash(msg ids)"),
     ("ack", "XACK · sample spans → eval queue")]
y = 90
for k, lbl in w:
    p.node(k, lbl, 720, y, 320, 56, kind="worker")
    y += 80
for a, b in zip([x[0] for x in w], [x[0] for x in w][1:]):
    p.edge(a, b, kind="flow")
p.node("dlq", "after 5 deliveries →<br>lumen:ingest:dlq (raw batch kept for replay)", 1100, 330, 260, 60, kind="note")
p.edge("dec", "dlq", "poison", kind="async")
p.edge("xadd", "claim", "stream", kind="async")

# ---------------------------------------------------------------------------- 3. ClickHouse model
p = doc.page("3. ClickHouse Data Model")
p.title("ClickHouse: wide insert-only spans + AggregatingMergeTree rollups",
        "No ReplacingMergeTree/FINAL on the hot table (Langfuse v4 lesson). Rollups make months of dashboards cheap.")
p.entity("spans", "spans (MergeTree)", [
    "ORDER BY (project_id, toStartOfMinute(start_time),", "  xxHash32(trace_id), span_id)",
    "PARTITION BY toYYYYMM · TTL 90d · SAMPLE BY xxHash32", "trace/span/parent ids, name, kind, status",
    "service, operation, provider, request/response model", "route, tenant, session_id, user_id, environment",
    "prompt_name, prompt_version, experiment_id, variant", "input/output/cache_read/cache_write/reasoning tokens",
    "cost_usd, cost_source, price_version, ttft_ms", "input/output_preview (ZSTD, opt-in)",
    "attributes Map · bloom(trace, session, user)"], 30, 90, 380)
p.entity("minute", "llm_minute (AggregatingMergeTree)", [
    "key: project, minute, tenant, route, provider,", "     model, prompt_name/version, variant",
    "calls/errors/cache_hits  SimpleAggregateFunction(sum)", "tokens, cost_usd          SimpleAggregateFunction(sum)",
    "latency  quantilesTDigest(0.5,0.95,0.99) state", "ttft     quantilesTDigest(0.5,0.95) state",
    "users    uniq state · TTL 400d"], 480, 90, 380)
p.entity("hour", "llm_hour (AggregatingMergeTree)", ["same columns, toStartOfHour", "merge-state MV from llm_minute",
                                                     "TTL 3 years"], 480, 330, 380)
p.entity("scores", "scores (ReplacingMergeTree(updated_at))", [
    "project_id, score_id, trace_id, span_id, session_id", "name, data_type (numeric|categorical|boolean)",
    "value, string_value, source (api|eval|annotation|feedback)", "comment, author · reads use argMax, not FINAL"],
         930, 90, 380)
p.entity("emb", "embedding_requests (MergeTree)", ["ts, model, input_type, backend", "items, tokens, cache_hits",
                                                   "batch_size, compute_ms · TTL 30d"], 930, 290, 380)
p.edge("spans", "minute", "llm_minute_mv (is_llm = 1)", kind="data")
p.edge("minute", "hour", "llm_hour_mv", kind="data")

# ---------------------------------------------------------------------------- 4. prompt registry
p = doc.page("4. Prompt Registry & SDK")
p.title("Prompt registry: immutable versions, movable labels, eval-gated promotion, SWR SDK cache")
p.node("author", "Author / CI", 40, 110, kind="client")
p.node("create", "POST /prompts/{name}/versions<br>compile · lint cache-friendliness · hash", 240, 100, 290, 70,
       kind="step")
p.node("ver", "prompt_versions<br>(UPDATE forbidden by trigger)", 600, 100, 220, 70, kind="db")
p.node("eval", "CI eval suite → POST .../evals<br>{metrics, passed}", 240, 230, 290, 60, kind="step")
p.node("gate", "PUT labels/production<br>passing eval for this version?", 600, 215, 240, 90, kind="decision")
p.node("set", "move label + label_history<br>PUBLISH invalidate", 900, 230, 230, 60, kind="step")
p.node("block", "409 promotion_blocked<br>(or override + reason, audited)", 900, 340, 230, 60, kind="end")
p.node("rb", "POST .../rollback → previous version", 600, 380, 240, 50, kind="step")
p.edge("author", "create", kind="flow")
p.edge("create", "ver", kind="data")
p.edge("eval", "gate", kind="flow")
p.edge("gate", "set", "yes", kind="flow")
p.edge("gate", "block", "no", kind="flow")
sdk = p.group("sdkg", "lumen-sdk PromptClient.get(name, label)", 40, 470, 1090, 150, kind="zone")
p.node("hit", "cached &amp; fresh (&lt;60s)<br>or pinned version → return", 20, 40, 230, 60, kind="step", parent=sdk)
p.node("stale", "stale → return immediately<br>+ background revalidate (ETag/304)", 280, 40, 260, 60, kind="step",
       parent=sdk)
p.node("miss", "miss → fetch (2s timeout)", 570, 40, 220, 60, kind="step", parent=sdk)
p.node("fb", "Lumen down → caller's fallback prompt<br>(observability never takes the product down)", 820, 40,
       250, 60, kind="note", parent=sdk)

# ---------------------------------------------------------------------------- 5. experiments
p = doc.page("5. Experiments & Statistics")
p.title("Experiments: deterministic assignment → spans tagged → SRM + Welch + sequential CI → auto-stop")
p.node("u", "request (unit = user | session | tenant)", 40, 100, 260, 50, kind="start")
p.node("a", "bucket = sha256(key:unit) mod 10000<br>(SDK and server: identical, no network)", 340, 90, 300, 70,
       kind="step")
p.node("t", "span attrs: lumen.experiment.id/variant<br>gen_ai.prompt.name/version", 690, 95, 300, 60, kind="step")
p.node("ch", "ClickHouse spans + scores", 1040, 90, 180, 70, kind="db")
p.node("an", "analyze(): units, metric mean/var, guardrails", 690, 230, 300, 50, kind="worker")
p.node("srm", "SRM χ² p &lt; 0.001?", 340, 220, 220, 70, kind="decision")
p.node("seq", "sequential CI (always-valid)<br>+ fixed-horizon Welch (report)", 340, 340, 260, 60, kind="step")
p.node("g", "guardrails: error rate Δ, cost ratio,<br>p95 latency ratio", 690, 340, 300, 60, kind="step")
p.node("stop", "auto-stop (harm / SRM / breach)<br>wins are shipped by humans", 340, 460, 260, 60, kind="end")
p.node("cuped", "CUPED (optional): Y − θ(X − x̄)<br>pre-period covariate, pooled θ", 690, 460, 300, 60, kind="note")
for a, b in [("u", "a"), ("a", "t"), ("t", "ch"), ("ch", "an"), ("an", "srm"), ("srm", "seq"), ("an", "g"),
             ("seq", "stop"), ("g", "stop")]:
    p.edge(a, b, kind="flow")

# ---------------------------------------------------------------------------- 6. online evals
p = doc.page("6. Online Evaluators")
p.title("Online evaluation cascade: cheap checks on every sampled span, LLM judge only where context exists")
p.node("s", "ingest consumer:<br>deterministic per-trace sampling", 40, 110, 240, 60, kind="worker")
p.node("q", "Redis list lumen:evals<br>(bounded, best-effort)", 330, 110, 220, 60, kind="queue")
p.node("cheap", "PII (regex + Luhn) · refusal ·<br>JSON validity · truncation", 600, 60, 260, 60, kind="step")
p.node("ctx", "captured &lt;sources&gt; context?", 600, 160, 260, 70, kind="decision")
p.node("j", "groundedness judge<br>(pinned 'judge' route, JSON schema)", 920, 160, 260, 60, kind="llm")
p.node("sc", "scores (source = eval)", 920, 60, 200, 60, kind="db")
p.node("low", "groundedness &lt; 0.5 →<br>annotation queue 'low-groundedness'", 920, 280, 260, 60, kind="step")
p.node("h", "human labels → scores (annotation)<br>→ Cohen's κ judge calibration", 600, 380, 300, 60, kind="note")
for a, b, lbl in [("s", "q", ""), ("q", "cheap", ""), ("q", "ctx", ""), ("ctx", "j", "yes"), ("j", "sc", ""),
                  ("cheap", "sc", ""), ("j", "low", ""), ("low", "h", "")]:
    p.edge(a, b, lbl, kind="flow")

# ---------------------------------------------------------------------------- 7. alerting
p = doc.page("7. Alerting")
p.title("Alerting: detectors are pure functions; notifications only on state transitions")
rows = [("slo", "SLO burn rate", "multi-window: 1h&amp;5m &gt; 14.4×, 6h&amp;30m &gt; 6×"),
        ("cost", "Cost anomaly", "robust z (median/MAD) vs same hour-of-week, min $"),
        ("q", "Quality drop", "24h mean score vs 7d baseline, n ≥ 30"),
        ("cache", "Cache-hit drop", "prompt-cache read ratio −50% vs 24h → unstable prefix"),
        ("drift", "Query drift", "embedding centroid cosine distance (daily, needs capture)")]
for i, (k, name, desc) in enumerate(rows):
    p.node(k, name, 40, 100 + i * 80, 180, 54, kind="service")
    p.node(k + "d", desc, 250, 100 + i * 80, 380, 54, kind="note")
p.node("eng", "evaluate_rules (arq cron, 1 min)", 700, 220, 240, 60, kind="worker")
p.node("tr", "state changed?", 1000, 210, 160, 80, kind="decision")
p.node("ev", "alert_events + webhook (Slack)", 1220, 220, 220, 60, kind="external")
for k, *_ in rows:
    p.edge(k + "d", "eng", kind="async")
p.edge("eng", "tr", kind="flow")
p.edge("tr", "ev", "yes", kind="flow")

# ---------------------------------------------------------------------------- 8. embeddings
p = doc.page("8. Embedding Service")
p.title("lumen-embedder: cache → single-flight → token-budget micro-batcher → TEI / OpenAI",
        "Drop-in TEI API (/embed) for Atlas, OpenAI-compatible /v1/embeddings, native /v1/embed (dims, int8/binary).")
seq = [("req", "request (inputs, model, input_type)", "start"),
       ("key", "key = emb:{model@version}:{type}:sha256(norm text)", "step"),
       ("mget", "Redis MGET (float16 vectors)", "decision"),
       ("sf", "single-flight: identical in-flight texts share one future", "step"),
       ("mb", "micro-batcher: queries before documents;<br>flush at padded-token budget or max_wait (5 ms)",
        "worker"),
       ("be", "backend call (TEI /embed | OpenAI /embeddings)", "llm"),
       ("post", "cache SET (TTL) · Matryoshka truncate · int8/binary", "step"),
       ("out", "response + request row → ClickHouse (10 s batches)", "end")]
y = 90
for k, lbl, kind in seq:
    p.node(k, lbl, 300, y, 420, 80 if kind == "decision" else 54, kind=kind)
    y += 105 if kind == "decision" else 80
for a, b in zip([s[0] for s in seq], [s[0] for s in seq][1:]):
    p.edge(a, b, "misses" if a == "mget" else "", kind="flow")
p.node("hit", "all hits → return (no backend)", 800, 255, 220, 50, kind="end")
p.edge("mget", "hit", "hit", kind="flow")
p.node("why", "<b>Why a wait window?</b><br>TEI's queue is strict FIFO with no wait: bursty single-text "
       "traffic yields tiny batches. A few ms of wait trades little latency for much higher "
       "throughput (bench/batching_sweep.py). Queue full → 429 (backpressure).", 800, 420, 330, 130, kind="note")

# ---------------------------------------------------------------------------- 9. deployment
p = doc.page("9. Deployment")
p.title("Deployment: compose profiles; one image, many roles; one-shot migrations gate the apps")
items = [("default", "postgres · redis(noeviction) · clickhouse · minio · migrate-postgres · migrate-clickhouse · "
                     "api · ingest · worker · embedder"),
         ("cpu", "tei-embed (bge-small) · tei-rerank (bge-reranker-base)"),
         ("gpu", "tei-embed-gpu (network alias tei-embed, NVIDIA reservation)"),
         ("observability", "otel-collector (tail sampling, redaction) · grafana (ClickHouse datasource, "
                           "dashboards-as-code)")]
for i, (name, desc) in enumerate(items):
    p.node(f"p{i}", f"profile: <b>{name}</b>", 40, 100 + i * 90, 200, 60, kind="service")
    p.node(f"d{i}", desc, 270, 100 + i * 90, 760, 60, kind="note")
p.node("k8s", "<b>Production notes</b><br>ClickHouse: managed or replicated cluster (ReplicatedMergeTree)<br>"
       "ingest consumers scale horizontally (consumer group)<br>embedder: HPA on queue depth; GPU nodes for TEI<br>"
       "S3 lifecycle rule replaces the retention cron", 1070, 100, 380, 150, kind="note")

# ---------------------------------------------------------------------------- 10. module map
p = doc.page("10. Code Module Map")
p.title("src/lumen: module dependencies (arrows = imports)",
        "Domain packages (telemetry, pricing, experiments, registry, evals, alerts, embeddings) never import api/.")
mods = [("api", "api/ (app, deps, routers)", 40, 100), ("emb_app", "embedder/ (app)", 40, 220),
        ("wk", "workers/ (arq crons)", 40, 340), ("ic", "ingest/ (service, consumer)", 40, 460),
        ("tel", "telemetry/ (otlp, semconv)", 380, 100), ("pr", "pricing/ (catalog)", 380, 200),
        ("ex", "experiments/ (assignment, stats)", 380, 300), ("rg", "registry/ (render, service)", 380, 400),
        ("evl", "evals/ (evaluators, calibration)", 380, 500), ("alr", "alerts/ (detectors, engine)", 380, 600),
        ("em", "embeddings/ (batcher, backends, service)", 380, 700),
        ("st", "storage/ (clickhouse, postgres, objects, models)", 760, 400),
        ("cm", "common/ (settings, logging, errors, telemetry)", 1100, 400)]
for k, lbl, x, yy in mods:
    p.node(k, lbl, x, yy, 290, 50, kind="component")
for a, b in [("api", "rg"), ("api", "ex"), ("api", "ic"), ("api", "st"), ("emb_app", "em"), ("wk", "evl"),
             ("wk", "alr"), ("wk", "ex"), ("ic", "tel"), ("tel", "pr"), ("ic", "st"), ("ex", "st"), ("rg", "st"),
             ("alr", "st"), ("st", "cm"), ("em", "cm")]:
    p.edge(a, b)

out = doc.save(Path(__file__).with_name("lumen.drawio"))
print(f"wrote {out}")
