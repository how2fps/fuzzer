"""
TEMPLATE: Copy to a new file (e.g. weighted.py), implement compute_power_schedule,
then in versions/__init__.py add:
    from . import weighted
    REGISTRY["weighted"] = weighted
"""
from __future__ import annotations

from typing import Sequence

from ..core import SeedStats, compute_power_schedule as base_compute


def compute_power_schedule(
    *,
    seeds: Sequence[SeedStats],
    min_energy: int = 1,
    max_energy: int = 128,
) -> dict[str, dict[int, int]]:
    """
    Compute how many mutations to run per seed.
    Return dict with key "seed_energies": ordinal -> mutation count.
    """
    # TODO: implement your policy (e.g. non-uniform counts per seed)
    return base_compute(
        seeds=seeds,
        min_energy=min_energy,
        max_energy=max_energy,
    )
