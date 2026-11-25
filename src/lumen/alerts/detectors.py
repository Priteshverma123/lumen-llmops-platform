"""Pure alert-detection math (unit-tested without any datastore).

* SLO burn rate, multi-window multi-burn-rate (Google SRE workbook): page when both a long and a
  short window burn fast, so alerts are fast to fire, fast to resolve and rarely flap.
* Cost anomaly: robust z-score (median/MAD; Iglewicz & Hoaglin) against a *weekly* baseline
  (same hour-of-week in previous weeks), because LLM traffic has strong weekday/hour seasonality
  and a plain trailing mean would page every Monday morning.
* Drift: cosine distance between the centroids of current and reference query embeddings.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Detection:
    firing: bool
    value: float
    message: str


# (long window, short window, burn-rate threshold): standard SRE workbook pairs for a 30-day SLO
BURN_WINDOWS = (("1h", "5m", 14.4), ("6h", "30m", 6.0))


def burn_rate(bad: float, total: float, slo_target: float) -> float:
    if total <= 0:
        return 0.0
    budget = 1.0 - slo_target
    return (bad / total) / budget if budget > 0 else math.inf


def multiwindow_burn(windows: dict[str, tuple[float, float]], slo_target: float) -> Detection:
    """windows: name → (bad_events, total_events) for 5m, 30m, 1h, 6h."""
    for long_w, short_w, threshold in BURN_WINDOWS:
        if long_w in windows and short_w in windows:
            bl, bs = burn_rate(*windows[long_w], slo_target), burn_rate(*windows[short_w], slo_target)
            if bl > threshold and bs > threshold:
                return Detection(True, bl, f"error budget burning {bl:.1f}x over {long_w} (and {bs:.1f}x over "
                                           f"{short_w}); threshold {threshold}x")
    worst = max((burn_rate(*v, slo_target) for v in windows.values()), default=0.0)
    return Detection(False, worst, "within error budget")


def robust_z(value: float, baseline: Sequence[float]) -> float:
    if len(baseline) < 3:
        return 0.0
    med = statistics.median(baseline)
    mad = statistics.median(abs(x - med) for x in baseline)
    if mad == 0:
        # flat baseline: fall back to mean absolute deviation so any real jump still registers
        mean_ad = sum(abs(x - med) for x in baseline) / len(baseline)
        return 0.0 if mean_ad == 0 and value == med else (value - med) / (mean_ad or max(abs(med) * 0.01, 1e-9))
    return 0.6745 * (value - med) / mad


def cost_anomaly(current: float, same_hour_previous_weeks: Sequence[float], trailing: Sequence[float],
                 threshold: float = 3.5, min_usd: float = 1.0) -> Detection:
    baseline = list(same_hour_previous_weeks) if len(same_hour_previous_weeks) >= 3 else list(trailing)
    z = robust_z(current, baseline)
    # ignore tiny absolute amounts: a jump from $0.02 to $0.20 is not worth a page
    firing = z > threshold and current >= min_usd
    med = statistics.median(baseline) if baseline else 0.0
    return Detection(firing, z, f"hourly spend ${current:.2f} vs typical ${med:.2f} (robust z={z:.1f})")


def quality_drop(current_mean: float, baseline_mean: float, current_n: int, max_drop: float = 0.05,
                 min_n: int = 30) -> Detection:
    drop = baseline_mean - current_mean
    firing = current_n >= min_n and drop > max_drop
    return Detection(firing, drop, f"quality {current_mean:.3f} vs baseline {baseline_mean:.3f} (n={current_n})")


def cache_hit_drop(current_ratio: float, baseline_ratio: float, max_relative_drop: float = 0.5) -> Detection:
    """Provider prompt-cache hit ratio collapsing usually means someone made the prompt prefix
    unstable (timestamp in the system prompt, reordered tools): a silent cost regression."""
    if baseline_ratio <= 0:
        return Detection(False, 0.0, "no baseline")
    rel = 1 - current_ratio / baseline_ratio
    return Detection(rel > max_relative_drop, rel,
                     f"cache-read ratio {current_ratio:.1%} vs baseline {baseline_ratio:.1%} ({rel:.0%} drop)")


def centroid(vectors: Sequence[Sequence[float]]) -> list[float]:
    dim = len(vectors[0])
    return [sum(v[i] for v in vectors) / len(vectors) for i in range(dim)]


def cosine_distance(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return 1.0 - dot / (na * nb) if na and nb else 0.0


def query_drift(current: Sequence[Sequence[float]], reference: Sequence[Sequence[float]],
                threshold: float = 0.15) -> Detection:
    if len(current) < 20 or len(reference) < 20:
        return Detection(False, 0.0, "not enough samples")
    d = cosine_distance(centroid(current), centroid(reference))
    return Detection(d > threshold, d, f"query embedding centroid moved {d:.3f} (cosine distance)")
