-- Scores are small and occasionally edited (annotations), so ReplacingMergeTree is acceptable here;
-- reads use argMax(..., updated_at) rather than FINAL.
CREATE TABLE IF NOT EXISTS scores
(
    project_id   LowCardinality(String),
    score_id     String,
    trace_id     String,
    span_id      String,
    session_id   String,
    name         LowCardinality(String),
    data_type    LowCardinality(String),   -- numeric | categorical | boolean
    value        Float64,
    string_value String,
    source       LowCardinality(String),   -- api | eval | annotation | feedback
    comment      String,
    author       String,
    config_id    String,
    created_at   DateTime64(3, 'UTC'),
    updated_at   DateTime64(3, 'UTC') DEFAULT now64(3),
    INDEX idx_trace trace_id TYPE bloom_filter(0.01) GRANULARITY 4
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY toYYYYMM(created_at)
ORDER BY (project_id, name, score_id)
TTL toDateTime(created_at) + INTERVAL 365 DAY;

CREATE TABLE IF NOT EXISTS embedding_requests
(
    ts             DateTime64(3, 'UTC'),
    model          LowCardinality(String),
    input_type     LowCardinality(String),
    backend        LowCardinality(String),
    items          UInt32,
    tokens         UInt32,
    cache_hits     UInt32,
    batch_size     UInt32,
    queue_wait_ms  Float32,
    compute_ms     Float32,
    status         LowCardinality(String)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (model, ts)
TTL toDateTime(ts) + INTERVAL 30 DAY;
