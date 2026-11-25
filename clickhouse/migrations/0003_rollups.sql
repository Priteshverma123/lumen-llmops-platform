-- Pre-aggregated rollups so dashboards over months of data read thousands of rows, not billions.
CREATE TABLE IF NOT EXISTS llm_minute
(
    project_id     LowCardinality(String),
    minute         DateTime('UTC'),
    tenant         LowCardinality(String),
    route          LowCardinality(String),
    provider       LowCardinality(String),
    model          LowCardinality(String),
    prompt_name    LowCardinality(String),
    prompt_version UInt32,
    variant        LowCardinality(String),
    calls          SimpleAggregateFunction(sum, UInt64),
    errors         SimpleAggregateFunction(sum, UInt64),
    cache_hits     SimpleAggregateFunction(sum, UInt64),
    input_tokens   SimpleAggregateFunction(sum, UInt64),
    output_tokens  SimpleAggregateFunction(sum, UInt64),
    cache_read_tokens SimpleAggregateFunction(sum, UInt64),
    cost_usd       SimpleAggregateFunction(sum, Float64),
    latency        AggregateFunction(quantilesTDigest(0.5, 0.95, 0.99), Float64),
    ttft           AggregateFunction(quantilesTDigest(0.5, 0.95), Float32),
    users          AggregateFunction(uniq, String)
)
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(minute)
ORDER BY (project_id, minute, tenant, route, provider, model, prompt_name, prompt_version, variant)
TTL minute + INTERVAL 400 DAY;

CREATE MATERIALIZED VIEW IF NOT EXISTS llm_minute_mv TO llm_minute AS
SELECT project_id, toStartOfMinute(start_time) AS minute, tenant, route, provider,
       if(response_model != '', response_model, request_model) AS model, prompt_name, prompt_version, variant,
       count() AS calls, countIf(status = 'error') AS errors, countIf(cache NOT IN ('', 'miss')) AS cache_hits,
       sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens,
       sum(cache_read_tokens) AS cache_read_tokens, sum(cost_usd) AS cost_usd,
       quantilesTDigestState(0.5, 0.95, 0.99)(duration_ms) AS latency,
       quantilesTDigestState(0.5, 0.95)(ttft_ms) AS ttft, uniqState(user_id) AS users
FROM spans WHERE is_llm = 1
GROUP BY project_id, minute, tenant, route, provider, model, prompt_name, prompt_version, variant;

CREATE TABLE IF NOT EXISTS llm_hour AS llm_minute
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(minute)
ORDER BY (project_id, minute, tenant, route, provider, model, prompt_name, prompt_version, variant)
TTL minute + INTERVAL 1095 DAY;

CREATE MATERIALIZED VIEW IF NOT EXISTS llm_hour_mv TO llm_hour AS
SELECT project_id, toStartOfHour(minute) AS minute, tenant, route, provider, model, prompt_name, prompt_version,
       variant, sum(calls) AS calls, sum(errors) AS errors, sum(cache_hits) AS cache_hits,
       sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens,
       sum(cache_read_tokens) AS cache_read_tokens, sum(cost_usd) AS cost_usd,
       quantilesTDigestMergeState(0.5, 0.95, 0.99)(latency) AS latency,
       quantilesTDigestMergeState(0.5, 0.95)(ttft) AS ttft, uniqMergeState(users) AS users
FROM llm_minute
GROUP BY project_id, minute, tenant, route, provider, model, prompt_name, prompt_version, variant;
