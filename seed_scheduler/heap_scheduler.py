from __future__ import annotations

from collections import deque
import heapq
from typing import Any, Literal, Sequence

from seed_corpus import Seed

from .base import BaseSeedScheduler
from .types import ScheduledSeed


PriorityMode = Literal["last_score", "avg_score"]


class HeapScheduler(BaseSeedScheduler):
    """
    Max-priority scheduler implemented on top of Python's min-heap.

    Items are popped via `next()` and reinserted on `update()` with a recomputed
    priority based on `isinteresting_score`.
    """

    def __init__(
        self,
        *,
        priority_mode: PriorityMode = "avg_score",
        bucket_prior: dict[str, float] | None = None,
        startup_min_batches_per_seed: int = 0,
    ) -> None:
        """Initialize heap-backed priority scheduling configuration and storage."""
        self._priority_mode = priority_mode
        self._bucket_prior = dict(bucket_prior or {})
        self._startup_min_batches_per_seed = max(0, int(startup_min_batches_per_seed))
        self._heap: list[tuple[float, int, str]] = []
        self._warm_queue: deque[str] = deque()
        self._warm_remaining: dict[str, int] = {}
        self._items: dict[str, ScheduledSeed] = {}
        self._seq = 0
        self._heap_counter = 0

    def add(self, seed: Seed, *, metadata: dict[str, Any] | None = None) -> ScheduledSeed:
        """Wrap a seed, compute its base priority, and push it onto the heap."""
        self._seq += 1
        item_id = f"h{self._seq:06d}"
        metadata_dict = dict(metadata or {})
        base_priority = float(self._bucket_prior.get(seed.bucket, 0.0))
        item = ScheduledSeed(
            item_id=item_id,
            seed=seed,
            priority=base_priority,
            metadata=metadata_dict,
        )
        initial_score = metadata_dict.get("initial_isinteresting_score")
        if initial_score is not None:
            score = float(initial_score)
            item.updates = 1
            item.total_isinteresting_score = score
            item.last_isinteresting_score = score
            item.priority = self._compute_priority(item)
        self._items[item_id] = item
        if self._should_warm_start(metadata_dict):
            self._warm_remaining[item_id] = self._startup_min_batches_per_seed
            self._warm_queue.append(item_id)
        else:
            self._push_heap(item)
        return item

    def next(self) -> ScheduledSeed:
        """Pop and return the highest-priority valid scheduled item."""
        while self._warm_queue:
            item_id = self._warm_queue.popleft()
            if item_id not in self._items:
                continue
            item = self._items[item_id]
            item.times_selected += 1
            return item
        while self._heap:
            _neg_prio, _order, item_id = heapq.heappop(self._heap)
            if item_id not in self._items:
                continue
            item = self._items[item_id]
            item.times_selected += 1
            return item
        raise IndexError("scheduler is empty")

    def empty(self) -> bool:
        """Return True when the heap has no ready entries."""
        return len(self) <= 0

    def __len__(self) -> int:
        """Return the number of heap entries currently ready for selection."""
        return len(self._ready_ids())

    def stats(self) -> dict[str, Any]:
        """Return priority scheduler metrics for logging and debugging."""
        return {
            "kind": "heap",
            "priority_mode": self._priority_mode,
            "warm_ready": sum(
                1 for item_id in self._warm_queue if item_id in self._items
            ),
            "ready": len(self),
            "total_items": len(self._items),
        }

    def ready_items(self) -> list[ScheduledSeed]:
        """Return unique ready items currently represented in the heap."""
        return [self._items[item_id] for item_id in self._ready_ids()]

    def debug_dump(self, limit: int = 20) -> dict[str, Any]:
        """Return a priority-ordered snapshot of tracked scheduler items."""
        # Show current items ordered by computed priority (highest first).
        ordered = sorted(
            self._items.values(),
            key=lambda item: (-item.priority, item.item_id),
        )[: max(limit, 0)]
        items = [
            {
                "item_id": item.item_id,
                "seed_id": item.seed.seed_id,
                "bucket": item.seed.bucket,
                "priority": item.priority,
                "times_selected": item.times_selected,
                "last_isinteresting_score": item.last_isinteresting_score,
                "avg_isinteresting_score": item.avg_isinteresting_score,
                "startup_batches_remaining": self._warm_remaining.get(item.item_id, 0),
            }
            for item in ordered
        ]
        return {
            "stats": self.stats(),
            "priority_order": items,
            "truncated": len(self._items) > len(items),
        }

    def complete_batch(self, item: ScheduledSeed, *, batch_scores: Sequence[float]) -> None:
        if batch_scores:
            n = len(batch_scores)
            item.updates += n
            item.total_isinteresting_score += float(sum(batch_scores))
            item.last_isinteresting_score = float(batch_scores[-1])
        item.priority = self._compute_priority(item)
        remaining = self._warm_remaining.get(item.item_id, 0)
        if remaining > 0:
            remaining -= 1
            if remaining > 0:
                self._warm_remaining[item.item_id] = remaining
                self._warm_queue.append(item.item_id)
            else:
                self._warm_remaining.pop(item.item_id, None)
                self._push_heap(item)
            return
        self._push_heap(item)

    def _compute_priority(self, item: ScheduledSeed) -> float:
        """Compute an item's effective priority from bucket bias and score history."""
        base = float(self._bucket_prior.get(item.seed.bucket, 0.0))
        if self._priority_mode == "last_score":
            return base + float(item.last_isinteresting_score or 0.0)
        return base + item.avg_isinteresting_score

    def _ready_ids(self) -> list[str]:
        ready_ids: list[str] = []
        seen: set[str] = set()
        for item_id in self._warm_queue:
            if item_id in self._items and item_id not in seen:
                ready_ids.append(item_id)
                seen.add(item_id)
        for _neg_prio, _order, item_id in self._heap:
            if item_id in self._items and item_id not in seen:
                ready_ids.append(item_id)
                seen.add(item_id)
        return ready_ids

    def _should_warm_start(self, metadata: dict[str, Any]) -> bool:
        if self._startup_min_batches_per_seed <= 0:
            return False
        return bool(
            metadata.get("startup_preloaded") or metadata.get("parent_seed_id")
        )

    def _push_heap(self, item: ScheduledSeed) -> None:
        """Push a scheduled item onto the heap using max-priority ordering."""
        self._heap_counter += 1
        # Negate for max-priority behavior using Python's min-heap.
        heapq.heappush(self._heap, (-item.priority, self._heap_counter, item.item_id))
