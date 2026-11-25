"""Typed configuration from the environment (LUMEN_ prefix)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LUMEN_", env_file=".env", extra="ignore")

    env: str = "dev"
    service_name: str = "lumen"
    log_level: str = "INFO"

    # datastores
    database_url: str = "postgresql+asyncpg://lumen:lumen@localhost:5432/lumen"
    redis_url: str = "redis://localhost:6379/0"
    clickhouse_url: str = "http://lumen:lumen@localhost:8123/lumen"
    s3_endpoint: str = "localhost:9000"
    s3_access_key: str = "lumen"
    s3_secret_key: SecretStr = SecretStr("lumen-secret")
    s3_bucket: str = "lumen"
    s3_secure: bool = False

    # ingest
    ingest_max_body_mb: int = 20
    ingest_batch_rows: int = 20_000  # ClickHouse likes big, infrequent inserts
    pricing_dir: Path = Path("pricing/manifests")
    semconv_version: str = "genai-2026.06"
    capture_content: bool = False  # prompt/response bodies off by default
    content_max_chars: int = 2000

    # online evals
    eval_sample_rate: float = 0.1
    judge_url: str = "http://atlas-gateway:8090"  # any OpenAI-compatible endpoint
    judge_route: str = "judge"
    judge_api_key: SecretStr = SecretStr("dev-internal-key")

    # alerts
    alert_webhook_url: str | None = None  # Slack incoming webhook or generic
    public_url: str = "http://localhost:3000"

    # embeddings
    embed_backends_file: Path = Path("config/embedding_models.yaml")
    embed_max_batch_tokens: int = 16_384
    embed_max_wait_ms: float = 5.0
    embed_cache_ttl_s: int = 30 * 86400
    rerank_url: str | None = "http://tei-rerank:80"
    embed_api_key: SecretStr | None = None  # optional bearer key for the embedder (internal network)

    # security
    bootstrap_token: SecretStr = SecretStr("change-me-bootstrap")
    otlp_endpoint: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
