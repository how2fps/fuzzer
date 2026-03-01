"""
TEMPLATE: Copy to a new file (e.g. my_scheduler.py), implement your scheduler class,
then in __init__.py:
  1. Add: from .my_scheduler import MyScheduler
  2. Add "my_scheduler" to SCHEDULER_KINDS (or a tuple of aliases)
  3. In make_scheduler(), add: if kind_normalized in {"my_scheduler", "my"}:
         return MyScheduler(**kwargs)
"""
from __future__ import annotations

from typing import Any

from seed_corpus import Seed

from .base import BaseSeedScheduler
from .types import ScheduledSeed


class TemplateScheduler(BaseSeedScheduler):
    """
    Implement: add, next, update, empty, __len__, stats.
    """

    def __init__(self, **kwargs: Any) -> None:
        # TODO: your state (e.g. queue, heap)
        _ = kwargs

    def add(self, seed: Seed, *, metadata: dict[str, Any] | None = None) -> ScheduledSeed:
        # TODO: create ScheduledSeed(item_id=..., seed=seed, ...), store it, return it
        raise NotImplementedError

    def next(self) -> ScheduledSeed:
        # TODO: return next item to fuzz
        raise NotImplementedError

    def update(
        self,
        item: ScheduledSeed,
        *,
        isinteresting_score: float,
        signals: dict[str, Any] | None = None,
    ) -> ScheduledSeed:
        # TODO: update item with score/signals, return the stored item
        raise NotImplementedError

    def empty(self) -> bool:
        # TODO: return True if no items to serve
        raise NotImplementedError

    def __len__(self) -> int:
        # TODO: return count of ready/active items
        raise NotImplementedError

    def stats(self) -> dict[str, Any]:
        # TODO: return dict of scheduler stats
        raise NotImplementedError
