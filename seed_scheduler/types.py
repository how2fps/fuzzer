from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from seed_corpus import Seed


@dataclass
class ScheduledSeed:
    item_id: str
    seed: Seed
    priority: float = 0.0
    times_selected: int = 0
    updates: int = 0
    last_isinteresting_score: float | None = None
    total_isinteresting_score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def avg_isinteresting_score(self) -> float:
        if self.updates == 0:
            return 0.0
        return self.total_isinteresting_score / self.updates

