"""
Registry of interestingness implementations for ablation / version selection.
"""
from __future__ import annotations

from collections.abc import Callable
from . import base

# Signature: (*, result: Mapping[str, Any]) -> float
ComputeInterestingnessFn = Callable[..., float]

REGISTRY: dict[str, ComputeInterestingnessFn] = {
    "base": base.compute_interestingness,
}


def get_compute_interestingness(version: str) -> ComputeInterestingnessFn:
    if version not in REGISTRY:
        raise ValueError(
            f"unknown isinteresting version {version!r}; choices: {sorted(REGISTRY)}"
        )
    return REGISTRY[version]


def list_versions() -> list[str]:
    return sorted(REGISTRY.keys())
