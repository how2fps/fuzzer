from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from seed_corpus import Seed

from .types import ScheduledSeed


class BaseSeedScheduler(ABC):
    @abstractmethod
    def add(self, seed: Seed, *, metadata: dict[str, Any] | None = None) -> ScheduledSeed:
        raise NotImplementedError

    @abstractmethod
    def next(self) -> ScheduledSeed:
        raise NotImplementedError

    @abstractmethod
    def update(
        self,
        item: ScheduledSeed,
        *,
        isinteresting_score: float,
        signals: dict[str, Any] | None = None,
    ) -> ScheduledSeed:
        raise NotImplementedError

    @abstractmethod
    def empty(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def __len__(self) -> int:
        raise NotImplementedError

    @abstractmethod
    def stats(self) -> dict[str, Any]:
        raise NotImplementedError
