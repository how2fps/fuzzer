"""
Constant power scheduler: assign the same mutation count to every seed.
"""
from __future__ import annotations

from typing import Sequence

from ..core import SeedStats


def compute_power_schedule(
    *,
    seeds: Sequence[SeedStats],
    min_energy: int = 1,
    max_energy: int = 128,
) -> dict[str, dict[int, int]]:
    """
    Constant power schedule.

    Every seed receives the same energy, clamped into [min_energy, max_energy].
    """
    if not seeds:
        return {"seed_energies": {}}

    min_e = max(1, min_energy)
    max_e = max(min_e, max_energy)

    # Use the midpoint of the allowed range as the constant energy.
    constant_energy = int(round((min_e + max_e) / 2.0))
    constant_energy = max(min_e, min(max_e, constant_energy))

    seed_energies: dict[int, int] = {}
    for s in seeds:
        seed_energies[int(s["id"])] = constant_energy

    return {"seed_energies": seed_energies}


__all__ = ["compute_power_schedule"]

