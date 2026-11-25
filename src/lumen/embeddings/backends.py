"""Embedding backends + model registry (``config/embedding_models.yaml``)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

from lumen.common.errors import NotFound, UpstreamError


@dataclass(slots=True)
class ModelSpec:
    id: str  # versioned id, e.g. "bge-small@1": part of every cache key
    backend: str  # tei | openai
    upstream_model: str
    dim: int
    url: str
    max_tokens: int = 512
    api_key_env: str | None = None
    query_prefix: str = ""
    document_prefix: str = ""
    price_per_mtok: float = 0.0
    aliases: list[str] = field(default_factory=list)


class TEIBackend:
    def __init__(self, spec: ModelSpec, client: httpx.AsyncClient) -> None:
        self.spec, self.http = spec, client

    async def __call__(self, texts: list[str]) -> list[list[float]]:
        try:
            r = await self.http.post(f"{self.spec.url.rstrip('/')}/embed",
                                     json={"inputs": texts, "normalize": True, "truncate": True})
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise UpstreamError(f"TEI error for {self.spec.id}: {exc}") from exc
        return r.json()  # type: ignore[no-any-return]


class OpenAIBackend:
    def __init__(self, spec: ModelSpec, client: httpx.AsyncClient) -> None:
        self.spec, self.http = spec, client
        self.key = os.environ.get(spec.api_key_env or "", "")

    async def __call__(self, texts: list[str]) -> list[list[float]]:
        try:
            r = await self.http.post(f"{self.spec.url.rstrip('/')}/embeddings",
                                     headers={"Authorization": f"Bearer {self.key}"},
                                     json={"model": self.spec.upstream_model, "input": texts})
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise UpstreamError(f"embedding API error for {self.spec.id}: {exc}") from exc
        data = sorted(r.json()["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in data]


class ModelRegistry:
    def __init__(self, specs: list[ModelSpec], default: str) -> None:
        self.specs = {s.id: s for s in specs}
        self.alias = {a: s.id for s in specs for a in [s.upstream_model, *s.aliases]}
        self.default = default

    @classmethod
    def load(cls, path: Path) -> ModelRegistry:
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        specs = [ModelSpec(**m) for m in raw["models"]]
        return cls(specs, raw.get("default", specs[0].id))

    def resolve(self, name: str | None) -> ModelSpec:
        key = name or self.default
        key = self.alias.get(key, key)
        if key not in self.specs:
            raise NotFound(f"unknown embedding model '{name}'", details={"models": sorted(self.specs)})
        return self.specs[key]
