"""Experiment statistics, dependency-free (stdlib math) so they are easy to audit and unit-test.

* SRM (sample-ratio mismatch): chi-square goodness of fit. An SRM means the assignment or
  logging is broken, and the result must not be trusted (GrowthBook practice).
* Fixed-horizon tests: two-proportion z-test; Welch's t-test (unequal variances).
* Sequential testing: asymptotic confidence sequences (Waudby-Smith et al., 2023, as used by
  GrowthBook). The interval is valid at *every* look, so teams can peek daily without inflating
  false positives. The price is a wider interval than a fixed-horizon test.
* CUPED (Deng et al., 2013): variance reduction using a pre-experiment covariate.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

# ----------------------------------------------------------------------------- special functions


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """Inverse normal CDF (Acklam's rational approximation, |error| < 1.2e-9)."""
    if not 0 < p < 1:
        raise ValueError("p must be in (0, 1)")
    a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02, 1.383577518672690e02,
         -3.066479806614716e01, 2.506628277459239e00]
    b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02, 6.680131188771972e01,
         -1.328068155288572e01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00, -2.549732539343734e00,
         4.374664141464968e00, 2.938163982698783e00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00, 3.754408661907416e00]
    lo, hi = 0.02425, 1 - 0.02425
    if p < lo:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
            ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > hi:
        return -norm_ppf(1 - p)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
        (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def _gammainc_upper_reg(s: float, x: float) -> float:
    """Q(s, x) = Γ(s, x)/Γ(s): series for x < s+1, continued fraction otherwise (Numerical Recipes)."""
    if x <= 0:
        return 1.0
    gln = math.lgamma(s)
    if x < s + 1:
        ap, total, delta = s, 1.0 / s, 1.0 / s
        for _ in range(500):
            ap += 1
            delta *= x / ap
            total += delta
            if abs(delta) < abs(total) * 1e-14:
                break
        return 1.0 - total * math.exp(-x + s * math.log(x) - gln)
    b, c, d = x + 1 - s, 1e300, 1.0 / (x + 1 - s)
    h = d
    for i in range(1, 500):
        an = -i * (i - s)
        b += 2
        d = an * d + b
        d = 1e-300 if abs(d) < 1e-300 else d
        c = b + an / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-14:
            break
    return math.exp(-x + s * math.log(x) - gln) * h


def chi2_sf(x: float, dof: int) -> float:
    return _gammainc_upper_reg(dof / 2.0, x / 2.0)


def _betacf(a: float, b: float, x: float) -> float:
    qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = 1e-300 if abs(d) < 1e-300 else d
    d = 1 / d
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1 + aa * d
        d = 1e-300 if abs(d) < 1e-300 else d
        c = 1 + aa / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1 + aa * d
        d = 1e-300 if abs(d) < 1e-300 else d
        c = 1 + aa / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-14:
            break
    return h


def betainc_reg(a: float, b: float, x: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    bt = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x))
    if x < (a + 1) / (a + b + 2):
        return bt * _betacf(a, b, x) / a
    return 1 - bt * _betacf(b, a, 1 - x) / b


def t_sf_two_sided(t: float, df: float) -> float:
    return betainc_reg(df / 2, 0.5, df / (df + t * t))


# ----------------------------------------------------------------------------- results


@dataclass(frozen=True, slots=True)
class TestResult:
    effect: float  # treatment - control (absolute)
    relative: float | None  # effect / control mean
    ci_low: float
    ci_high: float
    p_value: float | None  # None for confidence sequences (they're interval-based)
    significant: bool
    method: str


@dataclass(frozen=True, slots=True)
class SRMResult:
    chi2: float
    p_value: float
    mismatch: bool


def srm(observed: Sequence[int], weights: Sequence[float], threshold: float = 0.001) -> SRMResult:
    """Chi-square goodness of fit of observed unit counts vs configured weights.
    A strict threshold (p < 0.001) is standard: SRM checks run continuously on every experiment."""
    n = sum(observed)
    total_w = sum(weights)
    if n == 0:
        return SRMResult(0.0, 1.0, False)
    chi2 = sum((o - n * w / total_w) ** 2 / (n * w / total_w) for o, w in zip(observed, weights, strict=True))
    p = chi2_sf(chi2, len(observed) - 1)
    return SRMResult(chi2, p, p < threshold)


def two_proportion(x_c: int, n_c: int, x_t: int, n_t: int, alpha: float = 0.05) -> TestResult:
    p_c, p_t = x_c / n_c, x_t / n_t
    pooled = (x_c + x_t) / (n_c + n_t)
    se_pooled = math.sqrt(max(pooled * (1 - pooled) * (1 / n_c + 1 / n_t), 1e-18))
    z = (p_t - p_c) / se_pooled
    p = 2 * (1 - norm_cdf(abs(z)))
    se = math.sqrt(max(p_c * (1 - p_c) / n_c + p_t * (1 - p_t) / n_t, 1e-18))
    q = norm_ppf(1 - alpha / 2)
    diff = p_t - p_c
    return TestResult(diff, diff / p_c if p_c else None, diff - q * se, diff + q * se, p, p < alpha, "two_proportion_z")


def welch(mean_c: float, var_c: float, n_c: int, mean_t: float, var_t: float, n_t: int,
          alpha: float = 0.05) -> TestResult:
    vc, vt = var_c / n_c, var_t / n_t
    se = math.sqrt(max(vc + vt, 1e-18))
    t = (mean_t - mean_c) / se
    df = (vc + vt) ** 2 / ((vc**2) / max(n_c - 1, 1) + (vt**2) / max(n_t - 1, 1)) if (vc + vt) > 0 else 1.0
    p = t_sf_two_sided(t, df)
    q = norm_ppf(1 - alpha / 2) if df > 100 else _t_ppf(1 - alpha / 2, df)
    diff = mean_t - mean_c
    return TestResult(diff, diff / mean_c if mean_c else None, diff - q * se, diff + q * se, p, p < alpha, "welch_t")


def _t_ppf(p: float, df: float) -> float:
    lo, hi = 0.0, 50.0  # bisection on the CDF: plenty for CI construction
    for _ in range(80):
        mid = (lo + hi) / 2
        if 1 - t_sf_two_sided(mid, df) / 2 < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def sequential_ci(mean_c: float, var_c: float, n_c: int, mean_t: float, var_t: float, n_t: int,
                  alpha: float = 0.05, n_star: int = 5000) -> TestResult:
    """Asymptotic confidence sequence for the difference in means.

    With n = n_c + n_t and per-observation variance σ² = n·(var_c/n_c + var_t/n_t), the
    boundary is  σ·sqrt( 2(nρ²+1)/(n²ρ²) · ln( sqrt(nρ²+1)/α ) ),  where ρ is tuned so the
    sequence is tightest around the expected decision sample size ``n_star``.
    """
    n = n_c + n_t
    v = var_c / n_c + var_t / n_t
    sigma = math.sqrt(max(v * n, 1e-18))
    la = -2 * math.log(alpha)
    rho = math.sqrt((la + math.log(la + 1)) / n_star)
    width = sigma * math.sqrt(2 * (n * rho**2 + 1) / (n**2 * rho**2) * math.log(math.sqrt(n * rho**2 + 1) / alpha))
    diff = mean_t - mean_c
    lo, hi = diff - width, diff + width
    return TestResult(diff, diff / mean_c if mean_c else None, lo, hi, None, lo > 0 or hi < 0, "sequential_cs")


@dataclass(frozen=True, slots=True)
class CupedResult:
    theta: float
    adjusted_means: dict[str, float]
    adjusted_vars: dict[str, float]
    variance_reduction: float


def cuped(groups: dict[str, tuple[Sequence[float], Sequence[float]]]) -> CupedResult:
    """groups: variant → (metric Y, pre-period covariate X). θ is pooled across variants
    (required for unbiasedness); adjusted Y' = Y − θ(X − mean(X_all))."""
    all_y = [y for ys, _ in groups.values() for y in ys]
    all_x = [x for _, xs in groups.values() for x in xs]
    mx, my = sum(all_x) / len(all_x), sum(all_y) / len(all_y)
    cov = sum((x - mx) * (y - my) for x, y in zip(all_x, all_y, strict=True)) / max(len(all_x) - 1, 1)
    var_x = sum((x - mx) ** 2 for x in all_x) / max(len(all_x) - 1, 1)
    theta = cov / var_x if var_x > 0 else 0.0
    means, variances = {}, {}
    before, after = 0.0, 0.0
    for name, (ys, xs) in groups.items():
        adj = [y - theta * (x - mx) for y, x in zip(ys, xs, strict=True)]
        m_adj = sum(adj) / len(adj)
        means[name] = m_adj
        variances[name] = sum((a - m_adj) ** 2 for a in adj) / max(len(adj) - 1, 1)
        m_y = sum(ys) / len(ys)
        before += sum((y - m_y) ** 2 for y in ys)
        after += sum((a - m_adj) ** 2 for a in adj)
    return CupedResult(theta, means, variances, 1 - after / before if before else 0.0)


@dataclass(frozen=True, slots=True)
class GuardrailBreach:
    metric: str
    control: float
    treatment: float
    limit: float


def check_guardrails(control: dict[str, float], treatment: dict[str, float],
                     limits: dict[str, float]) -> list[GuardrailBreach]:
    """limits: {"error_rate": 0.02 (absolute increase), "cost_ratio": 1.3 (treatment/control),
    "p95_latency_ratio": 1.25}."""
    out = []
    if "error_rate" in limits and treatment.get("error_rate", 0) - control.get("error_rate", 0) > limits["error_rate"]:
        out.append(GuardrailBreach("error_rate", control.get("error_rate", 0), treatment["error_rate"],
                                   limits["error_rate"]))
    for m, key in (("cost_per_call", "cost_ratio"), ("p95_latency_ms", "p95_latency_ratio")):
        c, t = control.get(m, 0.0), treatment.get(m, 0.0)
        if key in limits and c > 0 and t / c > limits[key]:
            out.append(GuardrailBreach(m, c, t, limits[key]))
    return out
