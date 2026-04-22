"""
Hybrid power scheduler inspired by AFLFAST-style exploration/FAST switching.
"""
from __future__ import annotations

import math
from typing import Sequence

from ..core import SeedStats


ALPHA_RHO: float = 5.0
MAX_FAST_ENERGY: float = 1000.0
PLATEAU_THRESHOLD: int = 10
BREAKTHROUGH_LIMIT: int = 5

# Module-level state to approximate plateau detection and mode switches
_mode: str = "exploration"
_consecutive_no_gain: int = 0
_finds_in_fast_mode: int = 0
_total_arc_gains: int = 0


def _total_discovered_arc_gains(seeds: Sequence[SeedStats]) -> int:
    """Count cumulative edge/arc gains observed across all seeds."""
    return sum(max(0, int(seed.get("arc_gain_count", 0) or 0)) for seed in seeds)


def _update_mode(seeds: Sequence[SeedStats]) -> None:
    """Update global mode based on plateau / breakthrough heuristics."""
    global _mode, _consecutive_no_gain, _finds_in_fast_mode, _total_arc_gains

    arc_gains_now = _total_discovered_arc_gains(seeds)
    if arc_gains_now > _total_arc_gains:
        gained = arc_gains_now - _total_arc_gains
        _total_arc_gains = arc_gains_now
        _consecutive_no_gain = 0
        if _mode == "fast":
            _finds_in_fast_mode += gained
            if _finds_in_fast_mode >= BREAKTHROUGH_LIMIT:
                _mode = "exploration"
                _finds_in_fast_mode = 0
        return

    _consecutive_no_gain += 1
    if _mode == "exploration" and _consecutive_no_gain >= PLATEAU_THRESHOLD:
        _mode = "fast"
        _consecutive_no_gain = 0
        _finds_in_fast_mode = 0


def _compute_exploration_schedule(
    *,
    seeds: Sequence[SeedStats],
    min_energy: int,
    max_energy: int,
) -> dict[str, dict[int, int]]:
    """
    Exploration mode: balanced, near-uniform schedule.

    We give every seed the same weight and then scale/clamp into [min_energy, max_energy].
    """
    if not seeds:
        return {"seed_energies": {}}

    min_e = max(1, min_energy)
    max_e = max(min_e, max_energy)

    n = len(seeds)
    mean_energy = (min_e + max_e) / 2.0
    # All weights are 1.0, so total_w = n and scale = mean_energy.
    seed_energies: dict[int, int] = {}
    for s in seeds:
        raw = mean_energy
        energy = int(round(raw))
        energy = max(min_e, min(max_e, energy))
        seed_energies[int(s["id"])] = energy

    return {"seed_energies": seed_energies}


def _compute_fast_schedule(
    *,
    seeds: Sequence[SeedStats],
    min_energy: int,
    max_energy: int,
) -> dict[str, dict[int, int]]:
    """
    FAST mode: exponential schedule based on fuzz_count / bug_count.

    Heuristic adaptation of:
        E(t_i) = min((alpha/rho) * (2^s(i) / f(i)), M)
    using:
        s(i) ~= fuzz_count
        f(i) ~= 1 + avg_isinteresting_score + bug_count
    """
    if not seeds:
        return {"seed_energies": {}}

    min_e = max(1, min_energy)
    max_e = max(min_e, max_energy)

    weights: list[float] = []
    for s in seeds:
        fuzz_count = max(0, int(s.get("fuzz_count", 0)))
        bug_count = max(0, int(s.get("bug_count", 0)))
        avg_score = float(s.get("avg_isinteresting_score") or 0.0)
        recent_arc_novelty_rate = min(
            1.0, max(0.0, float(s.get("recent_arc_novelty_rate", 0.0) or 0.0))
        )
        same_arc_streak = max(0, int(s.get("same_arc_streak", 0)))
        arc_gain_count = max(0, int(s.get("arc_gain_count", 0)))

        s_i = min(fuzz_count, 10)  # cap exponent to keep numbers reasonable
        f_i = 1.0 + avg_score + (2.0 * recent_arc_novelty_rate)
        if bug_count > 0:
            f_i += min(float(bug_count), 5.0)
        if arc_gain_count > 0:
            f_i += min(math.log1p(float(arc_gain_count)), 5.0)
        f_i += math.log1p(float(same_arc_streak))

        energy_estimate = ALPHA_RHO * (math.pow(2.0, float(s_i)) / f_i)
        energy_estimate = min(energy_estimate, MAX_FAST_ENERGY)
        weights.append(max(energy_estimate, 1e-6))

    total_w = sum(weights)
    if total_w <= 0:
        total_w = 1.0

    n = len(seeds)
    mean_energy = (min_e + max_e) / 2.0
    scale = (mean_energy * n) / total_w

    seed_energies: dict[int, int] = {}
    for s, w in zip(seeds, weights):
        raw = w * scale
        energy = int(round(raw))
        energy = max(min_e, min(max_e, energy))
        seed_energies[int(s["id"])] = energy

    return {"seed_energies": seed_energies}


def compute_power_schedule(
    *,
    seeds: Sequence[SeedStats],
    min_energy: int = 1,
    max_energy: int = 128,
) -> dict[str, dict[int, int]]:
    """
    Hybrid exploration/FAST power scheduler.

    - In exploration mode, assign near-uniform energy to avoid early starvation.
    - After a plateau of calls with no new "interesting" seeds, switch to FAST mode.
    - In FAST mode, focus energy on seeds that have been fuzzed more and/or found bugs.
    """
    _update_mode(seeds)
    if _mode == "exploration":
        return _compute_exploration_schedule(
            seeds=seeds,
            min_energy=min_energy,
            max_energy=max_energy,
        )
    return _compute_fast_schedule(
        seeds=seeds,
        min_energy=min_energy,
        max_energy=max_energy,
    )


def _reset_state() -> None:
    global _mode, _consecutive_no_gain, _finds_in_fast_mode, _total_arc_gains
    _mode = "exploration"
    _consecutive_no_gain = 0
    _finds_in_fast_mode = 0
    _total_arc_gains = 0


__all__ = ["compute_power_schedule"]

