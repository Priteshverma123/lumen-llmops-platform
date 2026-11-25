import random
from collections import Counter

from lumen.experiments.assignment import Variant, assign
from lumen.experiments.stats import cuped, sequential_ci, srm, two_proportion, welch
from lumen_sdk.prompts import PromptClient

V = [Variant("control", 0.5), Variant("treatment", 0.5)]


def _mv(xs: list[float]) -> tuple[float, float]:
    m = sum(xs) / len(xs)
    return m, sum((x - m) ** 2 for x in xs) / (len(xs) - 1)


def test_assignment_is_deterministic():
    assert all(assign("exp1", f"u{i}", V) == assign("exp1", f"u{i}", V) for i in range(100))


def test_assignment_is_salted_per_experiment():
    a = [assign("exp1", f"u{i}", V) for i in range(500)]
    b = [assign("exp2", f"u{i}", V) for i in range(500)]
    assert a != b


def test_split_ratio_matches_weights():
    counts = Counter(assign("ratio", f"u{i}", [Variant("a", 0.2), Variant("b", 0.8)]) for i in range(20_000))
    assert abs(counts["a"] / 20_000 - 0.2) < 0.015


def test_sdk_and_server_assign_identically():
    sdk = PromptClient.__new__(PromptClient)
    for i in range(200):
        assert sdk.assign("parity", f"u{i}", [("control", 0.5), ("treatment", 0.5)]) == assign("parity", f"u{i}", V)


def test_srm_flags_broken_split():
    assert not srm([5000, 5050], [0.5, 0.5]).mismatch
    assert srm([5000, 5600], [0.5, 0.5]).mismatch


def test_two_proportion_and_welch_known_values():
    r = two_proportion(100, 1000, 130, 1000)
    assert abs((r.p_value or 0) - 0.0355) < 0.001 and r.significant
    w = welch(1.0, 1.0, 50, 1.5, 1.2, 50)
    assert abs((w.p_value or 0) - 0.0191) < 0.001


def test_sequential_ci_controls_false_positives_under_peeking():
    """A/A test peeked 20 times: the confidence sequence must keep the false-positive rate ≤ α."""
    rng = random.Random(7)
    hits, runs = 0, 200
    for _ in range(runs):
        c: list[float] = []
        t: list[float] = []
        for _ in range(20):
            c += [rng.gauss(0, 1) for _ in range(50)]
            t += [rng.gauss(0, 1) for _ in range(50)]
            (mc, vc), (mt, vt) = _mv(c), _mv(t)
            if sequential_ci(mc, vc, len(c), mt, vt, len(t)).significant:
                hits += 1
                break
    assert hits / runs <= 0.05


def test_sequential_detects_real_effect():
    rng = random.Random(1)
    (mc, vc), (mt, vt) = _mv([rng.gauss(0, 1) for _ in range(4000)]), _mv([rng.gauss(0.2, 1) for _ in range(4000)])
    assert sequential_ci(mc, vc, 4000, mt, vt, 4000).significant


def test_cuped_reduces_variance_with_correlated_covariate():
    rng = random.Random(3)
    groups = {}
    for name in ("control", "treatment"):
        xs = [rng.gauss(10, 3) for _ in range(2000)]
        groups[name] = ([x * 0.9 + rng.gauss(0, 1) for x in xs], xs)
    res = cuped(groups)
    assert res.variance_reduction > 0.7 and 0.8 < res.theta < 1.0
