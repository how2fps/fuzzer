from __future__ import annotations

import random
from typing import Mapping, MutableMapping, Sequence, TypedDict


class SeedStats(TypedDict):
    """
    Minimal per-seed information needed for AFL-style power scheduling.

    All numeric fields are expected to be non-negative.
    """

    # Stable identifier for the seed (queue index, filename hash, etc.)
    id: int
    # Optional execution time in milliseconds for this seed (average over runs).
    exec_time_ms: float | None
    # Optional per-edge bitmap for this seed (non-zero entries mean "edge hit").
    coverage_bitmap: Sequence[int] | None
    # How many times this seed has already been selected for fuzzing.
    fuzz_count: int


class ScheduleConfig(TypedDict, total=False):
    """
    Configuration for the power scheduler.

    - min_energy: lower bound for number of fuzzing attempts per seed.
    - max_energy: upper bound for number of fuzzing attempts per seed.
    """

    min_energy: int
    max_energy: int


class PowerScheduleResult(TypedDict):
    """
    Result of computing the power schedule.

    - seed_energies:   mapping seed_id -> number of fuzzing attempts ("energy").
    - edge_frequencies: how many seeds hit each edge (index-aligned with bitmaps).
    - config:          effective configuration (defaults + overrides).
    - total_weight:    sum of internal floating-point weights before clamping.
    """

    seed_energies: Mapping[int, int]
    edge_frequencies: Sequence[int]
    config: ScheduleConfig
    total_weight: float


DEFAULT_CONFIG: ScheduleConfig = {
    "min_energy": 1,
    "max_energy": 128,
}


def compute_edge_frequencies(*, seeds: Sequence[SeedStats]) -> list[int]:
    """
    Compute how many seeds hit each coverage edge.

    Bitmaps from all seeds are aligned by index. Any non-zero entry is treated
    as "edge covered" for that seed.
    """
    max_len = 0
    for stats in seeds:
        bitmap = stats.get("coverage_bitmap")
        if bitmap is None:
            continue
        if len(bitmap) > max_len:
            max_len = len(bitmap)

    if max_len == 0:
        return []

    frequencies = [0] * max_len
    for stats in seeds:
        bitmap = stats.get("coverage_bitmap")
        if bitmap is None:
            continue
        limit = min(len(bitmap), max_len)
        for idx in range(limit):
            if bitmap[idx]:
                frequencies[idx] += 1

    return frequencies


def _compute_seed_weight(
    *,
    seed: SeedStats,
) -> float:
    """
    Compute an unnormalized floating-point weight for a single seed.

    The weight is later turned into an integer "energy" (number of fuzzing
    attempts) via normalization and clamping.
    """
    # Uniform schedule: each seed starts with the same base weight.
    seed  # unused but kept for future extensions
    return 1.0


def compute_power_schedule(
    *,
    seeds: Sequence[SeedStats],
    config: ScheduleConfig | None = None,
) -> PowerScheduleResult:
    """
    Compute an AFL-style power schedule over the current queue.

    Input:
        seeds:  sequence of per-seed statistics.
        config: optional overrides for min_energy/max_energy.

    Output (RORO):
        {
            "seed_energies":   {seed_id: energy, ...},
            "edge_frequencies": [...],
            "config":          {...},
            "total_weight":    float,
        }

    You can feed the returned 'seed_energies' directly into your fuzzing loop
    to decide how many mutations to try for each seed in the queue.
    """
    if not seeds:
        effective_config: ScheduleConfig = DEFAULT_CONFIG.copy()
        if config:
            effective_config.update(config)
        return {
            "seed_energies": {},
            "edge_frequencies": [],
            "config": effective_config,
            "total_weight": 0.0,
        }

    effective_config: ScheduleConfig = DEFAULT_CONFIG.copy()
    if config:
        effective_config.update(config)
    min_energy = max(int(effective_config.get("min_energy", 1)), 1)
    max_energy = max(int(effective_config.get("max_energy", 128)), min_energy)

    edge_frequencies = compute_edge_frequencies(seeds=seeds)

    weights = [max(_compute_seed_weight(seed=seed), 0.0) for seed in seeds]

    # Fallback to uniform weights if everything collapsed to zero.
    if not any(weight > 0.0 for weight in weights):
        weights = [1.0] * len(seeds)

    total_weight = float(sum(weights))
    if total_weight <= 0.0:
        total_weight = float(len(weights))

    # Normalize weights so that the average energy sits roughly in the middle
    # of [min_energy, max_energy].
    avg_energy = (min_energy + max_energy) / 2.0
    current_mean_weight = total_weight / float(len(weights))
    scale = 1.0 if current_mean_weight <= 0.0 else (avg_energy / current_mean_weight)

    seed_energies: MutableMapping[int, int] = {}
    for stats, weight in zip(seeds, weights):
        raw_energy = weight * scale
        energy = int(raw_energy)
        if energy < min_energy:
            energy = min_energy
        if energy > max_energy:
            energy = max_energy
        seed_id = int(stats["id"])
        seed_energies[seed_id] = energy

    return {
        "seed_energies": seed_energies,
        "edge_frequencies": edge_frequencies,
        "config": effective_config,
        "total_weight": total_weight,
    }


def pick_seed_id(
    *,
    schedule: PowerScheduleResult,
    rng: random.Random | None = None,
) -> int | None:
    """
    Pick a seed identifier according to the computed energy weights.

    This is an optional helper you can use in your AFL-style main loop:

        stats = [...]  # list[SeedStats]
        sched = compute_power_schedule(seeds=stats)
        next_id = pick_seed_id(schedule=sched)

    Returns the selected seed_id, or None if there are no seeds.
    """
    seed_energies = schedule["seed_energies"]
    if not seed_energies:
        return None

    rng_engine = rng or random.Random()
    items = list(seed_energies.items())
    ids, energies = zip(*items)
    total = float(sum(energies))
    if total <= 0.0:
        return int(ids[rng_engine.randrange(len(ids))])

    threshold = rng_engine.random() * total
    cumulative = 0.0
    for seed_id, energy in items:
        cumulative += float(energy)
        if cumulative >= threshold:
            return int(seed_id)

    return int(ids[-1])

