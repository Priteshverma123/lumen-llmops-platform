"""ClickHouse access: async client, batched idempotent inserts, numbered migrations.

Insert rules (ClickHouse best practices + Langfuse incidents):
  * few large inserts (10k-100k rows) instead of many small ones → avoids "too many parts";
  * every insert chunk carries ``insert_deduplication_token`` = batch id + chunk index, so a
    worker that crashes after inserting and retries the same batch does not duplicate rows;
  * the app queries through parameter binding only (no string-formatted user input).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import clickhouse_connect
from clickhouse_connect.driver.asyncclient import AsyncClient

from lumen.common.logging import get_logger

log = get_logger(__name__)

SPAN_COLUMNS = [
    "project_id", "trace_id", "span_id", "parent_span_id", "start_time", "end_time", "duration_ms", "name", "kind",
    "status", "status_message", "service", "operation", "provider", "request_model", "response_model", "route",
    "tenant", "session_id", "user_id", "environment", "prompt_name", "prompt_version", "experiment_id", "variant",
    "cache", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
    "cost_usd", "cost_source", "price_version", "ttft_ms", "finish_reason", "is_llm", "input_preview",
    "output_preview", "attributes", "semconv_version", "ingest_batch",
]
SCORE_COLUMNS = ["project_id", "score_id", "trace_id", "span_id", "session_id", "name", "data_type", "value",
                 "string_value", "source", "comment", "author", "config_id", "created_at"]
EMBED_COLUMNS = ["ts", "model", "input_type", "backend", "items", "tokens", "cache_hits", "batch_size",
                 "queue_wait_ms", "compute_ms", "status"]


def _parts(url: str) -> dict[str, Any]:
    u = urlparse(url)
    return {"host": u.hostname or "localhost", "port": u.port or 8123, "username": u.username or "default",
            "password": u.password or "", "database": (u.path or "/default").lstrip("/") or "default",
            "secure": u.scheme == "https"}


async def connect(url: str, *, database: bool = True) -> AsyncClient:
    p = _parts(url)
    if not database:
        p["database"] = "default"
    return await clickhouse_connect.get_async_client(**p, settings={"async_insert": 0})


class ClickHouse:
    def __init__(self, client: AsyncClient) -> None:
        self.c = client

    async def insert_rows(self, table: str, columns: list[str], rows: list[dict[str, Any]], *,
                          batch_id: str, chunk: int = 20_000) -> int:
        n = 0
        for i in range(0, len(rows), chunk):
            part = rows[i:i + chunk]
            data = [[r.get(c) for c in columns] for r in part]
            await self.c.insert(table, data, column_names=columns,
                                settings={"insert_deduplication_token": f"{batch_id}:{table}:{i}"})
            n += len(part)
        return n

    async def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        res = await self.c.query(sql, parameters=params or {})
        return list(res.named_results())

    async def command(self, sql: str) -> Any:
        return await self.c.command(sql)

    async def ping(self) -> bool:
        return bool(await self.c.ping())

    async def close(self) -> None:
        await self.c.close()


async def migrate(url: str, migrations_dir: Path) -> list[str]:
    """Applies numbered ``*.sql`` files once, recording them in ``schema_migrations``."""
    db = _parts(url)["database"]
    admin = await connect(url, database=False)
    await admin.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    await admin.close()
    ch = await connect(url)
    await ch.command("CREATE TABLE IF NOT EXISTS schema_migrations (version String, applied_at DateTime DEFAULT "
                     "now()) ENGINE = MergeTree ORDER BY version")
    done = {r[0] for r in (await ch.query("SELECT version FROM schema_migrations")).result_rows}
    applied = []
    for f in sorted(migrations_dir.glob("*.sql")):
        if f.stem in done:
            continue
        statements = [s.strip() for s in _strip_comments(f.read_text(encoding="utf-8")).split(";") if s.strip()]
        for stmt in statements:
            await ch.command(stmt)
        await ch.insert("schema_migrations", [[f.stem]], column_names=["version"])
        applied.append(f.stem)
        log.info("clickhouse_migration_applied", version=f.stem)
    await ch.close()
    return applied


def _strip_comments(sql: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


if __name__ == "__main__":  # python -m lumen.storage.clickhouse  (one-shot migrate service)
    import asyncio

    from lumen.common.settings import get_settings

    print(asyncio.run(migrate(get_settings().clickhouse_url, Path("clickhouse/migrations"))))  # noqa: T201
