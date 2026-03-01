"""
Core power schedule implementation (uniform AFL-style). Used by versions.
"""
from __future__ import annotations

import random
from typing import Mapping, MutableMapping, Sequence, TypedDict


class SeedStats(TypedDict):
    """
    Minimal per-seed information needed for AFL-style power scheduling.

    All numeric fields are expected to be non-negative.
    """

    id: int
    exec_time_ms: float | None
    coverage_bitmap: Sequence[int] | None
    fuzz_count: int


class ScheduleConfig(TypedDict, total=False):
    min_energy: int
    max_energy: int


class PowerScheduleResult(TypedDict):
    seed_energies: Mapping[int, int]
    edge_frequencies: Sequence[int]
    config: ScheduleConfig
    total_weight: float


DEFAULT_CONFIG: ScheduleConfig = {
    "min_energy": 1,
    "max_energy": 128,
}


def compute_edge_frequencies(*, seeds: Sequence[SeedStats]) -> list[int]:
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


def _compute_seed_weight(*, seed: SeedStats) -> float:
    seed  # unused but kept for future extensions
    return 1.0


def compute_power_schedule(
    *,
    seeds: Sequence[SeedStats],
    config: ScheduleConfig | None = None,
) -> PowerScheduleResult:
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

    effective_config = DEFAULT_CONFIG.copy()
    if config:
        effective_config.update(config)
    min_energy = max(int(effective_config.get("min_energy", 1)), 1)
    max_energy = max(int(effective_config.get("max_energy", 128)), min_energy)

    edge_frequencies = compute_edge_frequencies(seeds=seeds)
    weights = [max(_compute_seed_weight(seed=seed), 0.0) for seed in seeds]

    if not any(weight > 0.0 for weight in weights):
        weights = [1.0] * len(seeds)

    total_weight = float(sum(weights))
    if total_weight <= 0.0:
        total_weight = float(len(weights))

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
