from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, Sequence

from seed_corpus import Seed

from .types import ScheduledSeed

DEFAULT_INTERESTING_SCORE_THRESHOLD = 0.5


class BaseSeedScheduler(ABC):
    """Common interface for scheduler backends that manage `ScheduledSeed` items."""

    def consider_seed(
        self,
        seed: Seed,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> ScheduledSeed | None:
        """
        Conditionally insert a seed and return the scheduled item when accepted.

        This is the scheduler-owned gate for worker-discovered seeds. Callers that
        want unconditional insertion, such as startup preload or refill paths,
        should continue to use `add(...)`.
        """
        metadata_dict = dict(metadata or {})
        if not self.should_schedule_seed(seed, metadata=metadata_dict):
            return None
        return self.add(seed, metadata=metadata_dict)

    def should_schedule_seed(
        self,
        seed: Seed,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """
        Return True when a candidate should enter the scheduler.

        Default policy:
        - reject duplicate seed texts already tracked by the scheduler
        - accept unconditional inserts when `scheduler_force_add` is set
        - for worker-discovered descendants (`parent_seed_id` present), only accept
          candidates that carry novelty via coverage, bug, or other first-seen
          behavioral signals already computed by workers
        - accept all other explicit callers
        """
        metadata_dict = dict(metadata or {})
        if self._has_seed_text(seed.text):
            return False
        if bool(metadata_dict.get("scheduler_force_add")):
            return True
        if not metadata_dict.get("parent_seed_id"):
            return True
        signals_raw = metadata_dict.get("signals")
        signals = signals_raw if isinstance(signals_raw, Mapping) else {}

        def _signal_score(name: str) -> float:
            raw = signals.get(name)
            try:
                return float(raw)
            except (TypeError, ValueError):
                return 0.0

        def _metadata_score(name: str) -> float:
            raw = metadata_dict.get(name)
            try:
                return float(raw)
            except (TypeError, ValueError):
                return 0.0

        interesting_score = max(
            _metadata_score("initial_isinteresting_score"),
            _metadata_score("isinteresting_score"),
            _signal_score("isinteresting"),
            _signal_score("isinteresting_score"),
        )
        has_strong_novelty = any(
            bool(signals.get(key))
            for key in (
                "new_coverage",
                "new_bug",
                "new_bug_site",
                "new_exception_site",
                "new_error_site",
                "new_differential_behavior",
            )
        )
        if has_strong_novelty:
            return True

        has_soft_semantic_novelty = (
            bool(_signal_score("input_structure_novelty") > 0.0)
            or bool(_signal_score("late_parse_depth") >= 0.6)
            or bool(_signal_score("partial_parse_success") > 0.0)
        )
        if not has_soft_semantic_novelty:
            return False
        return interesting_score >= DEFAULT_INTERESTING_SCORE_THRESHOLD

    @abstractmethod
    def add(self, seed: Seed, *, metadata: dict[str, Any] | None = None) -> ScheduledSeed:
        """Insert a new seed into the scheduler and return its wrapped scheduled item."""
        raise NotImplementedError

    @abstractmethod
    def next(self) -> ScheduledSeed:
        """Lease the next available scheduled item for mutation/execution."""
        raise NotImplementedError

    @abstractmethod
    def empty(self) -> bool:
        """Return True when the scheduler has no ready items to hand out."""
        raise NotImplementedError

    @abstractmethod
    def __len__(self) -> int:
        """Return the number of ready items currently available for selection."""
        raise NotImplementedError

    @abstractmethod
    def stats(self) -> dict[str, Any]:
        """Return a lightweight snapshot of scheduler state for logging/debugging."""
        raise NotImplementedError

    @abstractmethod
    def ready_items(self) -> list[ScheduledSeed]:
        """Return the currently schedulable items without mutating scheduler state."""
        raise NotImplementedError

    def debug_dump(self, limit: int = 20) -> dict[str, Any]:
        """
        Optional human-readable snapshot for debugging.
        Concrete schedulers can override with scheduler-specific structure.
        """
        return {"stats": self.stats(), "note": "debug_dump not implemented"}

    def supports_feedback_updates(self) -> bool:
        """Return True for schedulers that expect per-mutation `update(...)` calls."""
        return False

    def begin_batch(self, item: ScheduledSeed, *, batch_size: int) -> None:
        """
        Notify the scheduler that a leased item is about to run a mutation batch.

        Batch schedulers can ignore this. Feedback schedulers may use it to defer
        re-queuing until the full power-scheduled batch has completed.
        """
        return

    def complete_batch(
        self, item: ScheduledSeed, *, batch_scores: Sequence[float]
    ) -> None:
        """
        Re-queue a seed after a non-feedback (batch) lease finishes.

        Queue/heap schedulers pop items in `next()` and must put them back here.
        Feedback schedulers use `update(...)` per mutation instead; default is no-op.
        """
        return

    def update(
        self,
        item: ScheduledSeed,
        *,
        isinteresting_score: float,
        signals: dict[str, Any] | None = None,
    ) -> ScheduledSeed:
        """
        Optional per-mutation feedback hook.

        Feedback-driven schedulers should override this method and return the
        updated scheduled item.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support feedback updates"
        )

    def _has_seed_text(self, text: str) -> bool:
        items = getattr(self, "_items", None)
        if isinstance(items, dict):
            for item in items.values():
                item_seed = getattr(item, "seed", None)
                if item_seed is not None and getattr(item_seed, "text", None) == text:
                    return True
            return False
        return any(item.seed.text == text for item in self.ready_items())
