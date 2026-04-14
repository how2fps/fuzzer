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
from . import adaptive_all
from . import byte_havoc
from . import grammar_ast

REGISTRY: dict[str, Callable[..., str]] = {
    "adaptive_all": adaptive_all.mutate,
    "base": base.mutate,
    "byte_havoc": byte_havoc.mutate,
    "grammar_ast": grammar_ast.mutate,
}

FEEDBACK_REGISTRY: dict[str, FeedbackFn] = {}
if hasattr(adaptive_all, "handle_feedback"):
    FEEDBACK_REGISTRY["adaptive_all"] = adaptive_all.handle_feedback


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
