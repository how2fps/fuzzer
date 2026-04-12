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


class FeedbackFn(Protocol):
    def __call__(self, *, mutated_text: str, gained_coverage: bool) -> bool: ...


from . import base
from . import byte_havoc
from . import grammar_ast
from . import adaptive_all_baseline
from . import adaptive_all_experiment

REGISTRY: dict[str, Callable[..., str]] = {
    "base": base.mutate,
    "byte_havoc": byte_havoc.mutate,
    "grammar_ast": grammar_ast.mutate,
    "adaptive_all_baseline": adaptive_all_baseline.mutate,
    "adaptive_all_experiment": adaptive_all_experiment.mutate,
}

FEEDBACK_REGISTRY: dict[str, FeedbackFn] = {
    "adaptive_all_baseline": adaptive_all_baseline.handle_feedback,
    "adaptive_all_experiment": adaptive_all_experiment.handle_feedback,
}


def get_mutator(version: str) -> MutateFn:
    if version not in REGISTRY:
        raise ValueError(
            f"unknown mutator version {version!r}; choices: {sorted(REGISTRY)}"
        )
    return REGISTRY[version]


def list_versions() -> list[str]:
    return sorted(REGISTRY.keys())


def get_feedback_handler(version: str) -> FeedbackFn | None:
    return FEEDBACK_REGISTRY.get(version)
