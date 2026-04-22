"""
Power scheduler: determines how many mutations to run per seed.

Uses data from the fuzzer run DB (e.g. how often each seed has been used) and
AFL-style / good fuzzing practice: give more energy (mutations) to seeds that
have been fuzzed less and that are more "interesting."
"""
from __future__ import annotations

import math
from typing import Mapping, MutableMapping, Sequence, TypedDict


class SeedStats(TypedDict, total=False):
    """Per-seed stats from the corpus and optionally from the runs DB."""

    id: int
    fuzz_count: int
    avg_isinteresting_score: float
    bug_count: int
    recent_novelty_rate: float
    same_coverage_streak: int
    arc_gain_count: int
    recent_arc_novelty_rate: float
    same_arc_streak: int


def compute_power_schedule(
    *,
    seeds: Sequence[SeedStats],
    min_energy: int = 1,
    max_energy: int = 128,
) -> dict[str, Mapping[int, int]]:
    """
    Compute how many mutations to run per seed using DB-derived stats.

    AFL-style formula:
    - Seeds that have been fuzzed less get more energy (inverse of fuzz_count).
    - Optionally boost seeds with higher average interestingness or that found bugs.
    Returns dict with key "seed_energies": ordinal -> mutation count.
    """
    if not seeds:
        return {"seed_energies": {}}

    min_e = max(1, min_energy)
    max_e = max(min_e, max_energy)

    # Weight: favor under-fuzzed seeds and favor interesting / bug-finding seeds.
    # Base weight = 1 / (1 + fuzz_count) so never-fuzzed = 1.0, then decays.
    weights: list[float] = []
    for s in seeds:
        fuzz_count = int(s.get("fuzz_count", 0))
        base = 1.0 / (1.0 + fuzz_count)
        # Boost by average interestingness, but let recent novelty matter more
        # than stale historical score so repeated same-coverage paths cool off.
        avg_score = s.get("avg_isinteresting_score")
        if avg_score is not None and avg_score > 0:
            base *= (1.0 + math.log1p(float(avg_score)))
        recent_novelty_rate = float(s.get("recent_novelty_rate", 0.0) or 0.0)
        if recent_novelty_rate > 0.0:
            base *= (1.0 + (2.0 * min(recent_novelty_rate, 1.0)))
        same_coverage_streak = max(0, int(s.get("same_coverage_streak", 0)))
        if same_coverage_streak > 0:
            base /= (1.0 + math.log1p(float(same_coverage_streak)))
        # boost seeds that have found bugs
        bug_count = int(s.get("bug_count", 0))
        if bug_count > 0:
            base *= (1.0 + min(bug_count, 5))  # cap bonus
        weights.append(max(base, 1e-6))

    total_w = sum(weights)
    if total_w <= 0:
        total_w = 1.0
    n = len(seeds)
    # Scale weights so mean energy is in [min_e, max_e]; then clip per-seed to [min_e, max_e].
    mean_energy = (min_e + max_e) / 2.0
    scale = (mean_energy * n) / total_w if total_w > 0 else 1.0

    seed_energies: MutableMapping[int, int] = {}
    for s, w in zip(seeds, weights):
        raw = w * scale
        energy = int(round(raw))
        energy = max(min_e, min(max_e, energy))
        seed_energies[int(s["id"])] = energy

    return {"seed_energies": seed_energies}
