"""
Registry of mutator implementations for ablation / version selection.
"""
from __future__ import annotations

import random
from collections.abc import Callable
from typing import Protocol


class MutateFn(Protocol):
    def __call__(
        self,
        text: str,
        *,
        mutator_kind: str,
        rng: random.Random,
    ) -> str: ...

from . import base
from . import byte_havoc
from . import grammar_ast
from . import json_walk
from . import adaptive_all

REGISTRY: dict[str, Callable[..., str]] = {
    "base": base.mutate,
    "byte_havoc": byte_havoc.mutate,
    "grammar_ast": grammar_ast.mutate,
    "json_walk": json_walk.mutate,
    "adaptive_all": adaptive_all.mutate,
}


def get_mutator(version: str) -> MutateFn:
    if version not in REGISTRY:
        raise ValueError(
            f"unknown mutator version {version!r}; choices: {sorted(REGISTRY)}"
        )
    return REGISTRY[version]


def list_versions() -> list[str]:
    return sorted(REGISTRY.keys())
