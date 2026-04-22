from __future__ import annotations

from collections import deque
from typing import Any, Sequence

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
        """Initialize FIFO storage for scheduled items."""
        self._queue: deque[str] = deque()
        self._items: dict[str, ScheduledSeed] = {}
        self._seq = 0

    def add(self, seed: Seed, *, metadata: dict[str, Any] | None = None) -> ScheduledSeed:
        """Append a new seed to the tail of the queue."""
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
        """Pop and return the next scheduled item from the head of the queue."""
        if not self._queue:
            raise IndexError("scheduler is empty")
        item_id = self._queue.popleft()
        item = self._items[item_id]
        item.times_selected += 1
        return item

    def empty(self) -> bool:
        """Return True when no queued items remain."""
        return len(self._queue) == 0

    def __len__(self) -> int:
        """Return the number of queued items ready to be selected."""
        return len(self._queue)

    def complete_batch(self, item: ScheduledSeed, *, batch_scores: Sequence[float]) -> None:
        if batch_scores:
            n = len(batch_scores)
            item.updates += n
            item.total_isinteresting_score += float(sum(batch_scores))
            item.last_isinteresting_score = float(batch_scores[-1])
        self._queue.append(item.item_id)

    def stats(self) -> dict[str, Any]:
        """Return queue-oriented scheduler metrics."""
        return {
            "kind": "queue",
            "ready": len(self._queue),
            "total_items": len(self._items),
        }

    def ready_items(self) -> list[ScheduledSeed]:
        """Return queued items in dequeue order without consuming them."""
        return [
            self._items[item_id]
            for item_id in list(self._queue)
            if item_id in self._items
        ]

    def debug_dump(self, limit: int = 20) -> dict[str, Any]:
        """Return the current queue order with lightweight per-item metadata."""
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
