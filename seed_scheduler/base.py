from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from seed_corpus import Seed

from .types import ScheduledSeed


class BaseSeedScheduler(ABC):
    """Common interface for scheduler backends that manage `ScheduledSeed` items."""

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

    def debug_dump(self, limit: int = 20) -> dict[str, Any]:
        """
        Optional human-readable snapshot for debugging.
        Concrete schedulers can override with scheduler-specific structure.
        """
        return {"stats": self.stats(), "note": "debug_dump not implemented"}
