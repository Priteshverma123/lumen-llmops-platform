"""A/A simulation: proves the experiment statistics are calibrated.

Runs N A/A experiments (no true effect), peeking K times each, and reports how often each method
declares a "winner". A correct analysis keeps the false-positive rate at or below α:
  * fixed-horizon Welch t-test evaluated only at the end      → ≈ α
  * fixed-horizon test *peeked* at every look (the common mistake) → ≫ α
  * sequential confidence sequence peeked at every look        → ≤ α

    python bench/aa_simulation.py --runs 1000 --looks 20 --per-look 100
Write the printed table into docs/IMPACT.md / README as evidence.
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lumen.experiments.stats import sequential_ci, welch  # noqa: E402


def mv(xs: list[float]) -> tuple[float, float]:
    m = sum(xs) / len(xs)
    return m, sum((x - m) ** 2 for x in xs) / (len(xs) - 1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1000)
    ap.add_argument("--looks", type=int, default=20)
    ap.add_argument("--per-look", type=int, default=100)
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    fixed_end = peeking = seq = 0
    for _ in range(a.runs):
        c: list[float] = []
        t: list[float] = []
        peek_hit = seq_hit = False
        for _ in range(a.looks):
            c += [rng.gauss(0, 1) for _ in range(a.per_look)]
            t += [rng.gauss(0, 1) for _ in range(a.per_look)]
            (mc, vc), (mt, vt) = mv(c), mv(t)
            peek_hit |= welch(mc, vc, len(c), mt, vt, len(t), a.alpha).significant
            seq_hit |= sequential_ci(mc, vc, len(c), mt, vt, len(t), a.alpha).significant
        (mc, vc), (mt, vt) = mv(c), mv(t)
        fixed_end += welch(mc, vc, len(c), mt, vt, len(t), a.alpha).significant
        peeking += peek_hit
        seq += seq_hit
    print(f"A/A runs={a.runs} looks={a.looks} n/look/arm={a.per_look} alpha={a.alpha}")
    print("| method | false-positive rate |\n|---|---|")
    print(f"| fixed horizon, single look | {fixed_end / a.runs:.3f} |")
    print(f"| fixed horizon, peeked every look | {peeking / a.runs:.3f} |")
    print(f"| sequential CS, peeked every look | {seq / a.runs:.3f} |")


if __name__ == "__main__":
    main()
