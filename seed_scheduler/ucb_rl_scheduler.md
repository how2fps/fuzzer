# UCB / RL Scheduler Notes (Current: UCB Tree)

This document describes the current `ucb_tree` scheduler backend and the expected interface for a future RL scheduler.

## Current backend

Use:

```python
from seed_scheduler import make_scheduler

scheduler = make_scheduler("ucb_tree", ucb_c=1.0, max_seeds_per_leaf=8)
```

## What it stores

Tree structure:

- `root`
- `coverage bucket`
- `bug/output bucket`
- `seeds` (leaf bucket, capped)

Selection uses UCB1 down the tree.

## Expected `signals` (important)

Assume `signals` includes:

- `new_coverage: bool`
- `new_bug: bool`
- `crash: bool` and/or `timeout: bool` (or `status`)
- coverage bucketing data:
  - `coverage_key` (preferred), or
  - `coverage_signature`, or
  - `coverage_bitmap`
- bug/output bucketing data (optional but recommended):
  - `bug_signature`, or
  - `bug_key`

It can also accept your wrapped result shape:

```python
{
  "closed_result": {...},
  "open_result": {...}
}
```

The scheduler normalizes that internally.

## Reward used by UCB (computed inside scheduler from `signals`)

- `+1` if `new_coverage`
- `+2` if `new_bug`
- `+3` if crash/timeout

This reward updates `N` and `Q` on the selected path.

## Owner loop usage

```python
item = scheduler.next()

# worker runs a lease and returns summary + interesting candidates
lease_summary = {
    "new_coverage": True,
    "new_bug": False,
    "crash": False,
    "timeout": False,
    "status": "ok",
    "coverage_key": "cov:abc123",
    "bug_signature": None,
}

scheduler.update(item, isinteresting_score=0.0, signals=lease_summary)
```

Add new interesting candidates:

```python
scheduler.add(candidate_seed, metadata={"signals": candidate_signals})
```

## Notes for future RL scheduler

Keep the same owner-facing API:

- `add(seed, metadata=...)`
- `next()`
- `update(item, isinteresting_score=..., signals=...)`

RL can ignore `isinteresting_score` and learn from `signals`, or use both.

