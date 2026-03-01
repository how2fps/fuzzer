"""
TEMPLATE: Copy to a new file (e.g. weighted.py), implement compute_power_schedule,
then in versions/__init__.py add:
    from . import weighted
    REGISTRY["weighted"] = weighted
"""
from __future__ import annotations

from typing import Any, Sequence

from ..core import (
    PowerScheduleResult,
    ScheduleConfig,
    SeedStats,
)


def compute_power_schedule(
    *,
    seeds: Sequence[SeedStats],
    config: ScheduleConfig | None = None,
) -> PowerScheduleResult:
    """
    Compute seed_energies (seed_id -> number of fuzzing attempts) and related fields.

    Return dict with: seed_energies, edge_frequencies, config, total_weight.
    """
    # TODO: implement your power schedule (e.g. non-uniform weights)
    from ..core import DEFAULT_CONFIG, compute_edge_frequencies

    effective_config = dict(DEFAULT_CONFIG)
    if config:
        effective_config.update(config)
    edge_frequencies = compute_edge_frequencies(seeds=seeds)
    # Example: uniform energy per seed
    min_energy = max(int(effective_config.get("min_energy", 1)), 1)
    seed_energies = {int(s["id"]): min_energy for s in seeds}
    return {
        "seed_energies": seed_energies,
        "edge_frequencies": edge_frequencies,
        "config": effective_config,
        "total_weight": float(len(seeds)),
    }
