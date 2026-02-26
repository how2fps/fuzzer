from __future__ import annotations

from .base import BaseSeedScheduler
from .heap_scheduler import HeapScheduler
from .queue_scheduler import QueueScheduler
from .types import ScheduledSeed


def make_scheduler(kind: str, **kwargs) -> BaseSeedScheduler:
    kind_normalized = kind.strip().lower()
    if kind_normalized in {"queue"}:
        return QueueScheduler(**kwargs)
    if kind_normalized in {"heap"}:
        return HeapScheduler(**kwargs)
    raise ValueError(f"unknown scheduler kind {kind!r}")


__all__ = [
    "BaseSeedScheduler",
    "ScheduledSeed",
    "QueueScheduler",
    "HeapScheduler",
    "make_scheduler",
]
