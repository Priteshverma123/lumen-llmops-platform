"""Judge calibration: agreement between an LLM judge and human labels.

An LLM-as-judge score is only meaningful once you know how often it agrees with people. We
binarise both at a threshold and report Cohen's κ (agreement corrected for chance) plus
confusion counts. Rule of thumb: κ ≥ 0.6 substantial, ≥ 0.8 near-perfect; below 0.4, don't gate
anything on this judge.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Calibration:
    n: int
    accuracy: float
    kappa: float
    tp: int
    fp: int
    fn: int
    tn: int

    @property
    def verdict(self) -> str:
        return "near-perfect" if self.kappa >= 0.8 else "substantial" if self.kappa >= 0.6 else \
            "moderate" if self.kappa >= 0.4 else "unreliable"


def cohens_kappa(a: Sequence[int], b: Sequence[int]) -> float:
    if len(a) != len(b) or not a:
        raise ValueError("label sequences must be non-empty and equal length")
    n = len(a)
    labels = sorted(set(a) | set(b))
    po = sum(x == y for x, y in zip(a, b, strict=True)) / n
    pe = sum((a.count(lab) / n) * (b.count(lab) / n) for lab in labels)
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


def calibrate(judge: Sequence[float], human: Sequence[float], threshold: float = 0.5) -> Calibration:
    j = [int(x >= threshold) for x in judge]
    h = [int(x >= threshold) for x in human]
    tp = sum(1 for x, y in zip(j, h, strict=True) if x and y)
    tn = sum(1 for x, y in zip(j, h, strict=True) if not x and not y)
    fp = sum(1 for x, y in zip(j, h, strict=True) if x and not y)
    fn = sum(1 for x, y in zip(j, h, strict=True) if not x and y)
    return Calibration(len(j), (tp + tn) / len(j), cohens_kappa(j, h), tp, fp, fn, tn)
