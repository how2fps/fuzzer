from __future__ import annotations

from collections import deque
from typing import Any

from seed_corpus import Seed

from .base import BaseSeedScheduler
from .types import ScheduledSeed


class QueueScheduler(BaseSeedScheduler):
    """
    FIFO cyclic scheduler baseline.

    `next()` removes an item from the active queue and marks it in-flight.
    `update()` records the score and appends it to the tail (cycling behavior).
    """

    def __init__(self) -> None:
        self._queue: deque[str] = deque()
        self._items: dict[str, ScheduledSeed] = {}
        self._seq = 0

    def add(self, seed: Seed, *, metadata: dict[str, Any] | None = None) -> ScheduledSeed:
        self._seq += 1
        item_id = f"q{self._seq:06d}"
        item = ScheduledSeed(
            item_id=item_id,
            seed=seed,
            priority=0.0,
            metadata=dict(metadata or {}),
        )
        self._items[item_id] = item
        self._queue.append(item_id)
        return item

    def next(self) -> ScheduledSeed:
        if not self._queue:
            raise IndexError("scheduler is empty")
        item_id = self._queue.popleft()
        item = self._items[item_id]
        item.times_selected += 1
        return item

    def update(
        self,
        item: ScheduledSeed,
        *,
        isinteresting_score: float,
        signals: dict[str, Any] | None = None,
    ) -> ScheduledSeed:
        stored = self._items[item.item_id]
        stored.last_isinteresting_score = float(isinteresting_score)
        stored.total_isinteresting_score += float(isinteresting_score)
        stored.updates += 1
        if signals:
            stored.metadata["last_signals"] = signals
        self._queue.append(stored.item_id)
        return stored

    def empty(self) -> bool:
        return len(self._queue) == 0

    def __len__(self) -> int:
        return len(self._queue)

    def stats(self) -> dict[str, Any]:
        return {
            "kind": "queue",
            "ready": len(self._queue),
            "total_items": len(self._items),
        }

    def debug_dump(self, limit: int = 20) -> dict[str, Any]:
        ordered_ids = list(self._queue)[: max(limit, 0)]
        items = []
        for item_id in ordered_ids:
            item = self._items[item_id]
            items.append(
                {
                    "item_id": item.item_id,
                    "seed_id": item.seed.seed_id,
                    "bucket": item.seed.bucket,
                    "times_selected": item.times_selected,
                    "last_isinteresting_score": item.last_isinteresting_score,
                }
            )
        return {
            "stats": self.stats(),
            "queue_order": items,
            "truncated": len(self._queue) > len(items),
        }
