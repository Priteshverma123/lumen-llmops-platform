"""Deterministic, sticky variant assignment: no DB lookup on the hot path.

bucket = sha256(experiment_key ":" unit_id) mod 10_000. The experiment key salts the hash, so
assignments are independent across experiments (a user in "treatment" for one test is not
systematically in "treatment" for the next). Changing weights mid-flight only moves units whose
bucket crosses a boundary, and doing so is still discouraged; the SRM check catches mistakes.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

BUCKETS = 10_000


@dataclass(frozen=True, slots=True)
class Variant:
    name: str
    weight: float


def bucket(experiment_key: str, unit_id: str) -> int:
    digest = hashlib.sha256(f"{experiment_key}:{unit_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % BUCKETS


def assign(experiment_key: str, unit_id: str, variants: Sequence[Variant]) -> str:
    total = sum(v.weight for v in variants)
    if total <= 0:
        raise ValueError("variant weights must sum to > 0")
    b = bucket(experiment_key, unit_id)
    edge = 0.0
    for v in variants:
        edge += v.weight / total * BUCKETS
        if b < edge:
            return v.name
    return variants[-1].name
