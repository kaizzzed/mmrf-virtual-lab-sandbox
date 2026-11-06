"""Statistics the oracle computes itself: the log-rank test, the chi-square tail and Kaplan-Meier.

Nothing here comes from the platform's engines. `tests/test_stats.py` checks each function against
textbook values (chi-square critical values; the Freireich 6-MP leukemia trial as analysed by
Kleinbaum and Klein, "Survival Analysis", 3rd ed., chapter 2).
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

_EPS = 1e-15
_TINY = 1e-300
_MAX_ITERATIONS = 10_000


@dataclass(frozen=True)
class Observation:
    """One subject: the time to the event or to censoring, and whether the event happened."""

    time: float
    event: bool


def _gamma_series(a: float, x: float) -> float:
    """The regularized lower incomplete gamma P(a, x) by its series (x < a + 1)."""
    term = total = 1.0 / a
    denominator = a
    for _ in range(_MAX_ITERATIONS):
        denominator += 1.0
        term *= x / denominator
        total += term
        if abs(term) < abs(total) * _EPS:
            break
    return total * math.exp(-x + a * math.log(x) - math.lgamma(a))


def _gamma_continued_fraction(a: float, x: float) -> float:
    """The regularized upper incomplete gamma Q(a, x) by Lentz's continued fraction (x >= a + 1)."""
    b = x + 1.0 - a
    c = 1.0 / _TINY
    d = 1.0 / b
    h = d
    for i in range(1, _MAX_ITERATIONS):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        d = _TINY if abs(d) < _TINY else d
        c = b + an / c
        c = _TINY if abs(c) < _TINY else c
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _EPS:
            break
    return math.exp(-x + a * math.log(x) - math.lgamma(a)) * h


def chi2_sf(statistic: float, df: int) -> float:
    """P(X >= statistic) for X chi-square with `df` degrees of freedom."""
    if df < 1:
        raise ValueError("degrees of freedom must be at least 1")
    if statistic <= 0:
        return 1.0
    if df == 1:
        return math.erfc(math.sqrt(statistic / 2.0))
    if df == 2:
        return math.exp(-statistic / 2.0)
    a, x = df / 2.0, statistic / 2.0
    if x < a + 1.0:
        return max(0.0, 1.0 - _gamma_series(a, x))
    return _gamma_continued_fraction(a, x)


@dataclass(frozen=True)
class LogRankResult:
    statistic: float
    df: int
    p_value: float
    observed: tuple[int, ...]
    expected: tuple[float, ...]


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting; the matrix is small (groups - 1)."""
    size = len(vector)
    rows = [list(row) + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda r: abs(rows[r][col]))
        if abs(rows[pivot][col]) < 1e-300:
            raise ValueError("the log-rank covariance matrix is singular")
        rows[col], rows[pivot] = rows[pivot], rows[col]
        for r in range(col + 1, size):
            factor = rows[r][col] / rows[col][col]
            for k in range(col, size + 1):
                rows[r][k] -= factor * rows[col][k]
    solution = [0.0] * size
    for r in range(size - 1, -1, -1):
        solution[r] = (rows[r][size] - sum(rows[r][k] * solution[k] for k in range(r + 1, size))) / rows[r][r]
    return solution


def logrank(groups: Sequence[Sequence[Observation]]) -> LogRankResult:
    """The k-group log-rank (Mantel-Cox) test with the hypergeometric variance for tied times.

    At each distinct event time t: d events among n at risk; group j has d_j of its n_j at risk.
    E_j adds d * n_j / n; the covariance adds d * (n - d) / (n - 1) * (n_j / n) * (δ_jk - n_k / n).
    The statistic is (O - E)' V^-1 (O - E) over the first k - 1 groups, chi-square on k - 1 df.
    """
    k = len(groups)
    if k < 2:
        raise ValueError("the log-rank test needs at least two groups")
    times = sorted({obs.time for group in groups for obs in group if obs.event})
    observed = [sum(1 for obs in group if obs.event) for group in groups]
    expected = [0.0] * k
    covariance = [[0.0] * k for _ in range(k)]
    for t in times:
        at_risk = [sum(1 for obs in group if obs.time >= t) for group in groups]
        events = [sum(1 for obs in group if obs.event and obs.time == t) for group in groups]
        n, d = sum(at_risk), sum(events)
        if n == 0 or d == 0:
            continue
        for j in range(k):
            expected[j] += d * at_risk[j] / n
        if n > 1:
            scale = d * (n - d) / (n - 1)
            for j in range(k):
                for m in range(k):
                    share = (1.0 if j == m else 0.0) - at_risk[m] / n
                    covariance[j][m] += scale * (at_risk[j] / n) * share
    diff = [observed[j] - expected[j] for j in range(k - 1)]
    reduced = [row[: k - 1] for row in covariance[: k - 1]]
    solved = _solve(reduced, diff)
    statistic = sum(diff[j] * solved[j] for j in range(k - 1))
    return LogRankResult(statistic, k - 1, chi2_sf(statistic, k - 1), tuple(observed), tuple(expected))


@dataclass(frozen=True)
class KmPoint:
    time: float
    survival: float


def kaplan_meier(observations: Sequence[Observation]) -> list[KmPoint]:
    """The product-limit estimate after each distinct event time, starting at (0, 1)."""
    curve = [KmPoint(0.0, 1.0)]
    survival = 1.0
    for t in sorted({obs.time for obs in observations if obs.event}):
        at_risk = sum(1 for obs in observations if obs.time >= t)
        deaths = sum(1 for obs in observations if obs.event and obs.time == t)
        if at_risk:
            survival *= 1.0 - deaths / at_risk
            curve.append(KmPoint(t, survival))
    return curve


def km_median(observations: Sequence[Observation]) -> float | None:
    """The smallest time at which the Kaplan-Meier estimate is at most 0.5; None when never reached."""
    for point in kaplan_meier(observations):
        if point.survival <= 0.5 + 1e-12:
            return point.time
    return None
