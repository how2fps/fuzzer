"""
Registry of parser implementations for ablation / version selection.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from . import base


def _as_version(module: Any) -> SimpleNamespace:
    return SimpleNamespace(
        run_parser=module.run_parser,
        TARGETS=module.TARGETS,
        DEFAULT_TIMEOUT=module.DEFAULT_TIMEOUT,
    )


REGISTRY: dict[str, SimpleNamespace] = {
    "base": _as_version(base),
}


def get_parser(version: str) -> SimpleNamespace:
    if version not in REGISTRY:
        raise ValueError(
            f"unknown parser version {version!r}; choices: {sorted(REGISTRY)}"
        )
    return REGISTRY[version]


def list_versions() -> list[str]:
    return sorted(REGISTRY.keys())
