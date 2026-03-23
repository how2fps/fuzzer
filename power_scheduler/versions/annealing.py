"""
Annealing power scheduler: temperature-controlled softmax over feedback scores.

The scheduler keeps a module-level call counter as a proxy for run progress.
Temperature starts at 1.0 and cools toward T_MIN as the scheduler is recomputed
throughout the fuzzing run. Higher temperature yields a flatter allocation;
lower temperature makes the feedback score differences matter more.
"""
from __future__ import annotations

import math
from typing import Sequence

from ..core import SeedStats

T_INITIAL: float = 1.0
T_MIN: float = 0.15
COOLING_ALPHA: float = 0.97
COOLING_POWER: float = 2.0
MIN_WEIGHT: float = 1e-9

_schedule_calls: int = 0


def _normalized_progress() -> float:
    """
    Approximate budget progress from scheduler recomputation count.

    The exact run budget is not currently threaded through the power scheduler
    interface, so we map call count onto a saturating [0, 1) progress value.
    """
    if _schedule_calls <= 0:
        return 0.0
    return 1.0 - (1.0 / (1.0 + float(_schedule_calls)))


def _temperature() -> float:
    progress = _normalized_progress()
    # Equivalent to alpha^(progress^k), then clamped into [T_MIN, T_INITIAL].
    raw = math.pow(COOLING_ALPHA, math.pow(progress, COOLING_POWER))
    return max(T_MIN, min(T_INITIAL, raw))


def _seed_score(seed: SeedStats) -> float:
    fuzz_count = max(0, int(seed.get("fuzz_count", 0)))
    avg_score = float(seed.get("avg_isinteresting_score") or 0.0)
    bug_count = max(0, int(seed.get("bug_count", 0)))

    interesting_bonus = 1.5 * avg_score
    bug_bonus = 0.75 * math.log1p(float(bug_count))
    reuse_penalty = 0.30 * math.log1p(float(fuzz_count))

    return interesting_bonus + bug_bonus - reuse_penalty


def compute_power_schedule(
    *,
    seeds: Sequence[SeedStats],
    min_energy: int = 1,
    max_energy: int = 128,
) -> dict[str, dict[int, int]]:
    """
    Assign energy using a temperature-controlled softmax over feedback scores.

    score_i = 1.5 * avg_interestingness + 0.75 * log1p(bug_count)
              - 0.30 * log1p(fuzz_count)

    weight_i = exp(score_i / T)

    The normalized weights are then scaled into the allowed energy range.
    """
    global _schedule_calls

    if not seeds:
        return {"seed_energies": {}}

    _schedule_calls += 1

    min_e = max(1, min_energy)
    max_e = max(min_e, max_energy)
    mean_energy = (min_e + max_e) / 2.0
    temp = _temperature()

    scores = [_seed_score(seed) for seed in seeds]
    max_score = max(scores) if scores else 0.0

    # Stabilize exponentials by subtracting the max score before exponentiating.
    weights = [
        max(
            MIN_WEIGHT,
            math.exp((score - max_score) / max(temp, T_MIN)),
        )
        for score in scores
    ]

    total_w = sum(weights)
    if total_w <= 0:
        total_w = 1.0

    scale = (mean_energy * len(seeds)) / total_w
    seed_energies: dict[int, int] = {}
    for seed, weight in zip(seeds, weights):
        raw = weight * scale
        energy = int(round(raw))
        energy = max(min_e, min(max_e, energy))
        seed_energies[int(seed["id"])] = energy

    return {"seed_energies": seed_energies}


__all__ = ["compute_power_schedule"]
