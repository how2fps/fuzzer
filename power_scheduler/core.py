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
        # boost by average interestingness
        avg_score = s.get("avg_isinteresting_score")
        if avg_score is not None and avg_score > 0:
            base *= (1.0 + math.log1p(float(avg_score)))
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
