"""Versioned pricing manifests (Phoenix/Langfuse-style).

* Each manifest file has ``version``, ``effective_from`` and a ``sha256`` of its model list, so a
  cost row can always be traced to the exact price table that produced it.
* Models match by regex, most specific pattern first (``claude-haiku-5-5`` beats ``claude-.*``).
* Pricing tiers (e.g. long-context surcharges) apply above an input-token threshold.
* Cost is computed once at ingest; client-supplied cost always wins (the producer may know
  negotiated discounts we don't).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml


@dataclass(slots=True)
class Tier:
    above_input_tokens: int
    input: float
    output: float


@dataclass(slots=True)
class ModelPrice:
    provider: str
    pattern: re.Pattern[str]
    input: float
    output: float
    cache_read: float | None = None
    cache_write: float | None = None
    reasoning: float | None = None
    tiers: list[Tier] = field(default_factory=list)

    @property
    def specificity(self) -> int:
        return len(re.sub(r"[.*+?\\^$()\[\]{}|]", "", self.pattern.pattern))


@dataclass(slots=True)
class Manifest:
    version: str
    effective_from: datetime
    sha256: str
    models: list[ModelPrice]

    def find(self, provider: str, model: str) -> ModelPrice | None:
        for m in self.models:
            if (m.provider == provider or m.provider == "*") and m.pattern.fullmatch(model):
                return m
        return None


@dataclass(slots=True)
class CostResult:
    usd: float
    source: str  # client | inferred | unknown
    price_version: str


def _parse(raw: dict) -> Manifest:  # type: ignore[type-arg]
    models = [ModelPrice(provider=m["provider"], pattern=re.compile(m["match"]), input=float(m["input"]),
                         output=float(m["output"]), cache_read=m.get("cache_read"), cache_write=m.get("cache_write"),
                         reasoning=m.get("reasoning"), tiers=[Tier(**t) for t in m.get("tiers", [])])
              for m in raw["models"]]
    models.sort(key=lambda m: -m.specificity)
    digest = hashlib.sha256(json.dumps(raw["models"], sort_keys=True).encode()).hexdigest()
    eff = raw.get("effective_from", "1970-01-01")
    eff_dt = datetime.fromisoformat(str(eff)).replace(tzinfo=UTC) if isinstance(eff, str) else \
        datetime(eff.year, eff.month, eff.day, tzinfo=UTC)
    return Manifest(str(raw["version"]), eff_dt, digest, models)


class PriceCatalog:
    def __init__(self, manifests: list[Manifest]) -> None:
        if not manifests:
            raise ValueError("no pricing manifests")
        self.manifests = sorted(manifests, key=lambda m: m.effective_from)

    @classmethod
    def load_dir(cls, path: Path) -> PriceCatalog:
        return cls([_parse(yaml.safe_load(p.read_text(encoding="utf-8"))) for p in sorted(path.glob("*.yaml"))])

    @classmethod
    def from_dicts(cls, raws: list[dict]) -> PriceCatalog:  # type: ignore[type-arg]
        return cls([_parse(r) for r in raws])

    def manifest_at(self, ts: datetime) -> Manifest:
        chosen = self.manifests[0]
        for m in self.manifests:
            if m.effective_from <= ts:
                chosen = m
        return chosen

    def cost(self, *, provider: str, model: str, ts: datetime, input_tokens: int, output_tokens: int,
             cache_read: int = 0, cache_write: int = 0, reasoning: int = 0,
             client_cost: float | None = None) -> CostResult:
        """Token buckets must be disjoint (input excludes cache; output includes reasoning unless
        the model prices reasoning separately)."""
        man = self.manifest_at(ts)
        if client_cost is not None and client_cost >= 0:
            return CostResult(client_cost, "client", man.version)
        p = man.find(provider, model)
        if p is None:
            return CostResult(0.0, "unknown", man.version)
        inp, out = p.input, p.output
        total_in = input_tokens + cache_read + cache_write
        for t in sorted(p.tiers, key=lambda t: t.above_input_tokens):
            if total_in > t.above_input_tokens:
                inp, out = t.input, t.output
        cr = p.cache_read if p.cache_read is not None else inp
        cw = p.cache_write if p.cache_write is not None else inp
        reason_cost = 0.0
        out_tokens = output_tokens
        if p.reasoning is not None and reasoning:
            out_tokens = max(0, output_tokens - reasoning)
            reason_cost = reasoning * p.reasoning
        usd = (input_tokens * inp + out_tokens * out + cache_read * cr + cache_write * cw + reason_cost) / 1_000_000
        return CostResult(usd, "inferred", man.version)
