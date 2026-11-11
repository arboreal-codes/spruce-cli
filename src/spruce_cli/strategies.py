"""Quota strategies.

A strategy receives the used MB of every non-fixed site, the budget left for them (MB)
and the plan options, and returns the cap it used (or None) plus one Allocation per
site, in the same order. Fixed quotas, warning levels, CSV output and the summary are
handled by quota.build_plan, so a strategy only decides status, quota and target.

To add a strategy: write a function with the StrategyFn signature and register it in
STRATEGIES. The first line of its docstring is shown by `spruce quota strategies`.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class QuotaOptions:
    budget_tb: float
    reserve_percent: float = 10
    growth_margin_percent: float = 50
    min_quota_gb: float = 10
    freeze_margin_percent: float = 5
    warning_percent: float = 90


@dataclass(frozen=True)
class Allocation:
    status: str  # UnderCap | OverCap
    quota_mb: int
    target_mb: int

    def excess_mb(self, used_mb: int) -> int:
        return used_mb - self.target_mb if self.status == "OverCap" else 0


StrategyFn = Callable[[list[int], float, QuotaOptions], tuple[float | None, list[Allocation]]]


def water_fill_cap(sizes: list[float], budget: float) -> float | None:
    """Exact cap c with sum(min(s, c)) == budget, or None if everything already fits."""
    if sum(sizes) <= budget:
        return None
    if budget <= 0:
        raise ValueError("Budget for non-fixed sites is zero or negative.")
    ordered = sorted(sizes)
    prefix = 0.0
    for k, size in enumerate(ordered):
        cap = (budget - prefix) / (len(ordered) - k)
        lower = ordered[k - 1] if k > 0 else 0
        if lower <= cap <= size:
            return cap
        prefix += size
    raise ValueError("Unable to compute the water-filling cap.")


def whole_gb_in_mb(mb: float) -> int:
    return int(math.ceil(mb / 1024) * 1024)


def _with_growth(used_mb: int, options: QuotaOptions) -> float:
    return used_mb * (1 + options.growth_margin_percent / 100)


def _under_cap(raw_mb: float, options: QuotaOptions) -> Allocation:
    quota = max(whole_gb_in_mb(options.min_quota_gb * 1024), whole_gb_in_mb(raw_mb), 1024)
    return Allocation("UnderCap", quota, quota)


def _over_cap(used_mb: int, cap: float, options: QuotaOptions) -> Allocation:
    """Freeze at usage + small margin so the site stops growing without going read-only."""
    quota = whole_gb_in_mb(used_mb * (1 + options.freeze_margin_percent / 100))
    return Allocation("OverCap", quota, math.floor(cap))


def growth_margin(used: list[int], budget_mb: float, options: QuotaOptions) -> tuple[float | None, list[Allocation]]:
    """Water-filling cap; small sites get usage + growth margin (capped at c), big ones are frozen."""
    cap = water_fill_cap([float(u) for u in used], budget_mb)
    allocations = []
    for u in used:
        if cap is not None and u > cap:
            allocations.append(_over_cap(u, cap, options))
        else:
            grown = _with_growth(u, options)
            allocations.append(_under_cap(min(cap, grown) if cap is not None else grown, options))
    return cap, allocations


def water_filling(used: list[int], budget_mb: float, options: QuotaOptions) -> tuple[float | None, list[Allocation]]:
    """Pure water-filling: every site under the cap gets the full cap c as quota; big ones are frozen."""
    cap = water_fill_cap([float(u) for u in used], budget_mb)
    allocations = []
    for u in used:
        if cap is None:
            allocations.append(_under_cap(_with_growth(u, options), options))
        elif u > cap:
            allocations.append(_over_cap(u, cap, options))
        else:
            allocations.append(_under_cap(cap, options))
    return cap, allocations


def budget_fill(used: list[int], budget_mb: float, options: QuotaOptions) -> tuple[float | None, list[Allocation]]:
    """Water-filling over demand (usage + growth margin): sum(min(demand, c)) == budget, so quotas track the budget."""
    demand = [_with_growth(u, options) for u in used]
    cap = water_fill_cap(demand, budget_mb)
    allocations = []
    for u, d in zip(used, demand, strict=True):
        if cap is not None and u > cap:
            allocations.append(_over_cap(u, cap, options))
        else:
            allocations.append(_under_cap(min(d, cap) if cap is not None else d, options))
    return cap, allocations


STRATEGIES: dict[str, StrategyFn] = {
    "growth-margin": growth_margin,
    "water-filling": water_filling,
    "budget-fill": budget_fill,
}
DEFAULT_STRATEGY = "growth-margin"


def describe(name: str) -> str:
    return (STRATEGIES[name].__doc__ or "").strip().splitlines()[0]
