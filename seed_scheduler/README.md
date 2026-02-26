# Seed Scheduler (Usage)

Swappable scheduler backends for fuzz loop (`queue`, `heap`, `ucb_tree`).

## Architecture (important)

This scheduler is intended to be owned by a single coordinator/owner process.

- Owner process/thread:
  - calls `next()`
  - sends parent seed to a worker
  - receives worker result
  - calls `update(...)`
  - calls `add(...)` for newly interesting seeds
- Worker process:
  - owns the mutator
  - owns the target runner
  - owns the **power scheduler** (local mutation budget / loop)
  - returns a summary + interesting candidates

The seed scheduler does **not** manage mutation budgets. That belongs to each worker's power scheduler.

## Create a scheduler

```python
from seed_scheduler import make_scheduler

scheduler = make_scheduler("queue")  # FIFO baseline
# or
scheduler = make_scheduler("heap", priority_mode="avg_score")
# or
scheduler = make_scheduler("ucb_tree", ucb_c=1.0, max_seeds_per_leaf=8)
```

## Add a seed from seed corpus

```python
import random
from seed_corpus import SeedCorpus

corpus = SeedCorpus.load()
batch = corpus.sample_ratio_batch(
    "cidrize-runner",
    total=50,
    bucket_ratios={"valid": 0.7, "string_stress": 0.2, "near_valid": 0.1},
    rng=random.Random(42),
    shuffle=True,
)
seed = batch[0]
scheduler.add(seed)
```

## Main loop pattern (important)

```python
while not scheduler.empty():
    item = scheduler.next()

    # Your fuzzer logic:
    input_text = item.seed.text
    # mutate -> run target -> parse result
    # drop in examples till run result is finalized
    run_result = {"exit_code": 0}
    isinteresting_score = 0.73

    scheduler.update(
        item,
        isinteresting_score=isinteresting_score,
        signals=run_result,
    )
```

In your multi-worker design, `update(...)` should use a **worker lease summary** (not every mutation event).
For example, a worker can run many mutations locally, then return:

- parent summary `isinteresting_score` (e.g. max score seen during the lease)
- summary signals (`crash_count`, unique outputs, etc.)
- list of interesting mutated candidates

Then the owner does:

```python
scheduler.update(item, isinteresting_score=lease_max_score, signals=lease_summary)

for candidate_seed in interesting_candidate_seeds:
    scheduler.add(candidate_seed)
```

## What each scheduler does

- `queue`: FIFO cyclic baseline (score is recorded, order stays FIFO)
- `heap`: priority-based (score updates item priority)
- `ucb_tree`: tree buckets (`coverage -> bug/output -> seeds`) selected with UCB1

`heap` `priority_mode` options:

- `"avg_score"` (default): running average `isinteresting_score`
- `"last_score"`: most recent `isinteresting_score`

`ucb_tree` notes:

- `update(...)` computes reward from `signals` (`new_coverage`, `new_bug`, `crash`/`timeout`)
- `isinteresting_score` is accepted for API compatibility but UCB updates use signal-derived reward
- for bucket placement on `add(...)`, pass hints via `metadata={"signals": ...}`

## Helpful methods

- `scheduler.add(seed)`
- `scheduler.next()`
- `scheduler.update(item, isinteresting_score=..., signals=...)`
- `scheduler.empty()`
- `scheduler.stats()`
- `scheduler.debug_dump(limit=20)` (inspect current scheduler contents)

## Inspect current scheduler contents (debug)

Use `debug_dump()` to see what is currently inside the scheduler.

```python
print(scheduler.debug_dump(limit=10))
```

What it returns depends on the backend:

- `queue`: current queue order (`item_id`, `seed_id`, bucket, stats)
- `heap`: current priority order (priority + score stats)
- `ucb_tree`: leaf buckets (`coverage_key`, `bug_key`, leaf `N/Q`, seed IDs)

## Demo

```bash
python3 -m seed_scheduler.demo
```
