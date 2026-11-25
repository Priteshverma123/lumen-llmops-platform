-- Wide, insert-only span table (Langfuse v4 lesson: no ReplacingMergeTree/FINAL on the hot table;
-- denormalise trace-level attributes onto every span so queries never join).
CREATE TABLE IF NOT EXISTS spans
(
    project_id         LowCardinality(String),
    trace_id           String,
    span_id            String,
    parent_span_id     String,
    start_time         DateTime64(6, 'UTC'),
    end_time           DateTime64(6, 'UTC'),
    duration_ms        Float64,
    name               LowCardinality(String),
    kind               LowCardinality(String),
    status             LowCardinality(String),
    status_message     String,
    service            LowCardinality(String),
    operation          LowCardinality(String),
    provider           LowCardinality(String),
    request_model      LowCardinality(String),
    response_model     LowCardinality(String),
    route              LowCardinality(String),
    tenant             LowCardinality(String),
    session_id         String,
    user_id            String,
    environment        LowCardinality(String),
    prompt_name        LowCardinality(String),
    prompt_version     UInt32,
    experiment_id      LowCardinality(String),
    variant            LowCardinality(String),
    cache              LowCardinality(String),
    input_tokens       UInt32,
    output_tokens      UInt32,
    cache_read_tokens  UInt32,
    cache_write_tokens UInt32,
    reasoning_tokens   UInt32,
    cost_usd           Float64,
    cost_source        LowCardinality(String),
    price_version      LowCardinality(String),
    ttft_ms            Float32,
    finish_reason      LowCardinality(String),
    is_llm             UInt8,
    input_preview      String CODEC(ZSTD(3)),
    output_preview     String CODEC(ZSTD(3)),
    attributes         Map(LowCardinality(String), String),
    semconv_version    LowCardinality(String),
    ingest_batch       String,
    inserted_at        DateTime64(3, 'UTC') DEFAULT now64(3),
    INDEX idx_trace   trace_id   TYPE bloom_filter(0.01) GRANULARITY 4,
    INDEX idx_session session_id TYPE bloom_filter(0.01) GRANULARITY 4,
    INDEX idx_user    user_id    TYPE bloom_filter(0.01) GRANULARITY 4
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(start_time)
ORDER BY (project_id, toStartOfMinute(start_time), xxHash32(trace_id), span_id)
SAMPLE BY xxHash32(trace_id)
TTL toDateTime(start_time) + INTERVAL 90 DAY
SETTINGS index_granularity = 8192,
         -- enables insert_deduplication_token on non-replicated tables → idempotent retries
         non_replicated_deduplication_window = 1000;
