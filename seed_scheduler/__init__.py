from __future__ import annotations

from .base import BaseSeedScheduler
from .heap_scheduler import HeapScheduler
from .queue_scheduler import QueueScheduler
from .types import ScheduledSeed
from .ucb_tree_scheduler import UCBTreeScheduler


SCHEDULER_KINDS = ("queue", "heap", "ucb_tree")


def make_scheduler(kind: str, **kwargs) -> BaseSeedScheduler:
    kind_normalized = kind.strip().lower()
    if kind_normalized in {"queue"}:
        return QueueScheduler(**kwargs)
    if kind_normalized in {"heap"}:
        return HeapScheduler(**kwargs)
    if kind_normalized in {"ucb_tree", "ucb", "tree"}:
        return UCBTreeScheduler(**kwargs)
    raise ValueError(f"unknown scheduler kind {kind!r}")


def get_scheduler(version: str, **kwargs) -> BaseSeedScheduler:
    """Return a scheduler instance for the given version (for ablation)."""
    return make_scheduler(version, **kwargs)


def list_versions() -> list[str]:
    """Return available scheduler version names."""
    return list(SCHEDULER_KINDS)


__all__ = [
    "BaseSeedScheduler",
    "HeapScheduler",
    "QueueScheduler",
    "SCHEDULER_KINDS",
    "ScheduledSeed",
    "UCBTreeScheduler",
    "get_scheduler",
    "list_versions",
    "make_scheduler",
]
