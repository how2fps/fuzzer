from __future__ import annotations

from .base import BaseSeedScheduler
from .heap_scheduler import HeapScheduler
from .queue_scheduler import QueueScheduler
from .types import ScheduledSeed
from .ucb_tree_scheduler import UCBTreeScheduler


def make_scheduler(kind: str, **kwargs) -> BaseSeedScheduler:
    kind_normalized = kind.strip().lower()
    if kind_normalized in {"queue"}:
        return QueueScheduler(**kwargs)
    if kind_normalized in {"heap"}:
        return HeapScheduler(**kwargs)
    if kind_normalized in {"ucb_tree", "ucb", "tree"}:
        return UCBTreeScheduler(**kwargs)
    raise ValueError(f"unknown scheduler kind {kind!r}")


__all__ = [
    "BaseSeedScheduler",
    "ScheduledSeed",
    "QueueScheduler",
    "HeapScheduler",
    "UCBTreeScheduler",
    "make_scheduler",
]
