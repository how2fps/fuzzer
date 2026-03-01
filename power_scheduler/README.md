## PowerScheduler module

This package contains a small, AFL-style power scheduler that you can plug into any fuzzing loop. It is independent of the mutator and parser layers and works with simple, typed dictionaries.

The scheduler is **uniform**: every seed starts with the same base weight, and the module converts those weights into per-seed "energy" (how many fuzzing attempts to spend on that seed).

### Installation / import

Inside this repo:

```python
from power_scheduler import (
    SeedStats,
    ScheduleConfig,
    PowerScheduleResult,
    compute_edge_frequencies,
    compute_power_schedule,
    pick_seed_id,
)
```

Everything is plain functions with type hints; there is no class state to manage.

### Data model

- `SeedStats`: minimal per-seed statistics needed for power scheduling:

```python
from typing import Sequence

class SeedStats(TypedDict):
    id: int                          # stable seed identifier (queue index, filename hash, etc.)
    exec_time_ms: float | None       # optional average exec time in milliseconds
    coverage_bitmap: Sequence[int] | None  # optional per-edge bitmap (non-zero means "edge hit")
    fuzz_count: int                  # how many times this seed was already fuzzed
```

- `ScheduleConfig`: configuration for the scheduler:

```python
class ScheduleConfig(TypedDict, total=False):
    min_energy: int                  # minimum number of fuzzing iterations per seed
    max_energy: int                  # maximum number of fuzzing iterations per seed
```

If you do not pass a config, the defaults are:

```python
DEFAULT_CONFIG: ScheduleConfig = {
    "min_energy": 1,
    "max_energy": 128,
}
```

- `PowerScheduleResult`: full RORO result from `compute_power_schedule`:

```python
class PowerScheduleResult(TypedDict):
    seed_energies: Mapping[int, int]   # seed_id -> energy
    edge_frequencies: Sequence[int]    # how many seeds hit each edge index
    config: ScheduleConfig             # effective config (defaults + overrides)
    total_weight: float                # internal sum of weights before clamping
```

### Core functions

- **Compute edge frequencies** (optional helper):

```python
from power_scheduler import compute_edge_frequencies

edge_freqs = compute_edge_frequencies(seeds=seed_stats)
```

This walks all `coverage_bitmap` values and counts, for each edge index, how many seeds hit that edge at least once. The scheduler itself is currently uniform, but `edge_freqs` is still reported for diagnostics and extensions.

- **Compute power schedule**:

```python
from power_scheduler import compute_power_schedule

schedule = compute_power_schedule(
    seeds=seed_stats,
    config={
        "min_energy": 2,
        "max_energy": 64,
    },
)
```

`seed_stats` is a `list[SeedStats]`. The function returns a `PowerScheduleResult`:

- `seed_energies[seed_id]` is the number of fuzzing attempts you should allocate to that seed in the next cycle.
- Energies are normalized so that the average lies between `min_energy` and `max_energy`, then clamped per seed.

- **Pick a seed according to energy**:

```python
from power_scheduler import pick_seed_id

seed_id = pick_seed_id(schedule=schedule)
```

This draws a seed identifier at random, with probability proportional to its assigned energy. If there are no seeds, it returns `None`.

### Example AFL-style loop sketch

```python
import random
from typing import List

from mutator import mutate_json_input
from parser import run_parser
from power_scheduler import SeedStats, compute_power_schedule, pick_seed_id


def fuzz_loop(seed_corpus: list[str], max_iterations: int) -> None:
    # Initialise simple per-seed stats.
    stats: List[SeedStats] = [
        {
            "id": idx,
            "exec_time_ms": None,
            "coverage_bitmap": None,
            "fuzz_count": 0,
        }
        for idx, _ in enumerate(seed_corpus)
    ]

    for _ in range(max_iterations):
        schedule = compute_power_schedule(seeds=stats)
        seed_id = pick_seed_id(schedule=schedule)
        if seed_id is None:
            break

        entry = stats[seed_id]
        original = seed_corpus[seed_id]
        mutated = mutate_json_input(original_text=original)
        payload = mutated.encode("utf-8")

        result = run_parser(input_data=payload, target="json-decoder", timeout=5.0)
        entry["fuzz_count"] += 1

        # Optional: update coverage_bitmap and exec_time_ms in 'entry'
        # from 'result' if/when you expose that information.
```

You can extend this skeleton by:

- Tracking coverage bitmaps per seed (from your targets) and feeding them into `SeedStats["coverage_bitmap"]`.
- Recording average execution times per seed in `exec_time_ms` and using that for future non-uniform schedulers.

