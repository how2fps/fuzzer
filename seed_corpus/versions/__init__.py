"""
Registry of seed corpus implementations for ablation / version selection.
"""
from __future__ import annotations

from typing import Any, Type

from . import base

# Type: class with load() classmethod returning SeedCorpus instance
CorpusLoader = Type[Any]

REGISTRY: dict[str, CorpusLoader] = {
    "base": base.SeedCorpus,
}


def get_corpus_loader(version: str) -> CorpusLoader:
    if version not in REGISTRY:
        raise ValueError(
            f"unknown seed_corpus version {version!r}; choices: {sorted(REGISTRY)}"
        )
    return REGISTRY[version]


def list_versions() -> list[str]:
    return sorted(REGISTRY.keys())
