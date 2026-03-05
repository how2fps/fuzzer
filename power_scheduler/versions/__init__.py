"""
Registry of power scheduler implementations for ablation / version selection.
"""
from __future__ import annotations

from typing import Any

from . import base, hybrid, constant

REGISTRY: dict[str, Any] = {
    "base": base,
    "hybrid": hybrid,
    "constant": constant,
}


def get_power_scheduler(version: str) -> Any:
    if version not in REGISTRY:
        raise ValueError(
            f"unknown power_scheduler version {version!r}; choices: {sorted(REGISTRY)}"
        )
    return REGISTRY[version]


def list_versions() -> list[str]:
    return sorted(REGISTRY.keys())
