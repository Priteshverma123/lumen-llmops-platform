"""Prompt client with stale-while-revalidate caching (the hot path must never wait on Lumen).

Cache policy (Langfuse/MLflow practice):
  * pinned versions are immutable → cached forever;
  * labels (``production``) move → fresh for 60 s, then served *stale* while one background
    refresh revalidates with ETag (304 = no body);
  * if Lumen is unreachable and nothing is cached → the caller's ``fallback`` prompt is used, so
    an observability outage can never become a product outage.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from lumen_sdk.tracing import record_experiment, record_prompt


@dataclass(frozen=True)
class Prompt:
    name: str
    version: int
    messages: list[dict[str, str]]
    model_config: dict[str, Any] = field(default_factory=dict)
    is_fallback: bool = False

    def render(self, **variables: Any) -> list[dict[str, str]]:
        """Simple ``{{ var }}`` substitution client-side (server-side /render uses full sandboxed
        Jinja2 + schema validation; use it when templates need logic)."""
        out = []
        for m in self.messages:
            content = m["content"]
            for k, v in variables.items():
                content = content.replace("{{ " + k + " }}", str(v)).replace("{{" + k + "}}", str(v))
            out.append({"role": m["role"], "content": content})
        return out


@dataclass
class _Entry:
    prompt: Prompt
    etag: str
    fetched: float


class PromptClient:
    def __init__(self, base_url: str, api_key: str, *, label_ttl_s: float = 60.0, timeout_s: float = 2.0) -> None:
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_s,
                                       headers={"Authorization": f"Bearer {api_key}"})
        self.ttl = label_ttl_s
        self._cache: dict[tuple[str, str], _Entry] = {}
        self._refreshing: set[tuple[str, str]] = set()

    async def _fetch(self, name: str, ref: str, etag: str | None) -> _Entry | None:
        params = {"version": ref[1:]} if ref.startswith("v") and ref[1:].isdigit() else {"label": ref}
        headers = {"If-None-Match": etag} if etag else {}
        r = await self._http.get(f"/v1/prompts/{name}", params=params, headers=headers)
        if r.status_code == 304:
            return None
        r.raise_for_status()
        d = r.json()
        return _Entry(Prompt(d["prompt"], d["version"], d["messages"], d.get("model_config", {})),
                      r.headers.get("ETag", ""), time.monotonic())

    async def _revalidate(self, key: tuple[str, str]) -> None:
        try:
            old = self._cache.get(key)
            new = await self._fetch(key[0], key[1], old.etag if old else None)
            if new is not None:
                self._cache[key] = new
            elif old is not None:
                old.fetched = time.monotonic()
        except httpx.HTTPError:
            pass  # keep serving stale; retry on next access after TTL
        finally:
            self._refreshing.discard(key)

    async def get(self, name: str, *, label: str = "production", version: int | None = None,
                  fallback: list[dict[str, str]] | None = None) -> Prompt:
        key = (name, f"v{version}" if version is not None else label)
        entry = self._cache.get(key)
        if entry is not None:
            immutable = version is not None
            if not immutable and time.monotonic() - entry.fetched > self.ttl and key not in self._refreshing:
                self._refreshing.add(key)
                asyncio.get_running_loop().create_task(self._revalidate(key))
            record_prompt(entry.prompt.name, entry.prompt.version)
            return entry.prompt
        try:
            fetched = await self._fetch(name, key[1], None)
            assert fetched is not None
            self._cache[key] = fetched
            record_prompt(fetched.prompt.name, fetched.prompt.version)
            return fetched.prompt
        except (httpx.HTTPError, AssertionError):
            if fallback is None:
                raise
            return Prompt(name, 0, fallback, is_fallback=True)

    def assign(self, experiment_key: str, unit_id: str, variants: list[tuple[str, float]]) -> str:
        """Local, deterministic assignment (same algorithm as the server: sha256 mod 10_000), so
        assigning a variant costs no network call. Also tags the current span."""
        digest = hashlib.sha256(f"{experiment_key}:{unit_id}".encode()).digest()
        b = int.from_bytes(digest[:8], "big") % 10_000
        total, edge = sum(w for _, w in variants), 0.0
        chosen = variants[-1][0]
        for name, w in variants:
            edge += w / total * 10_000
            if b < edge:
                chosen = name
                break
        record_experiment(experiment_key, chosen)
        return chosen

    async def aclose(self) -> None:
        await self._http.aclose()
