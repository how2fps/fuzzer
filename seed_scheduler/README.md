# Seed Scheduler (Usage)

Swappable scheduler backends for fuzz loop (`queue`, `heap`, `ucb_tree`, `thompson`).

## Architecture (important)

This scheduler is intended to be owned by a single coordinator/owner process.

- Owner process/thread:
  - calls `next()`
  - sends parent seed to a worker
  - calls `add(...)` for newly interesting seeds
- Owner process/thread with `ucb_tree`:
  - receives one mutation result
  - calls `update(...)` for that mutation
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
# or
scheduler = make_scheduler("thompson", rng_seed=42)
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
    # add newly interesting mutated children back with scheduler.add(...)
```

For feedback-driven schedulers like `ucb_tree` and `thompson`, one `next()`
call is one bandit pull and one mutation attempt. After that single mutation
finishes, call `update(...)` immediately with that mutation result. Then the
owner does:

```python
item = scheduler.next()
mutated_text = mutate(item.seed.text)
result = run_target(mutated_text)
score = compute_isinteresting(result)

scheduler.update(item, isinteresting_score=score, signals=result)

if score > 0:
    scheduler.add(candidate_seed, metadata={"signals": result})
```

## What each scheduler does

- `queue`: FIFO one-shot baseline
- `heap`: priority-based ordering at insertion time
- `ucb_tree`: tree buckets (`coverage -> bug/output -> seeds`) selected with UCB1 and updated once per mutation result
- `thompson`: Thompson Sampling over execution features; uses coverage edges
  when available and falls back to differential/bug/status features for
  black-box targets

`heap` `priority_mode` options:

- `"avg_score"` (default)
- `"last_score"`

`ucb_tree` notes:

- `update(...)` computes reward from `signals` (`new_coverage`, `new_bug`, `crash`/`timeout`)
- `next()` selects one parent for one mutation attempt
- `isinteresting_score` is accepted but UCB updates use signal-derived reward
- for bucket placement on `add(...)`, pass hints via `metadata={"signals": ...}`

`thompson` notes:

- keeps Beta(`alpha`, `beta`) posteriors per execution feature
- success is a novel result (`new_coverage`, `new_bug`, or `new_differential_behavior`)
- coverage targets use covered edges as features
- black-box / oracle targets fall back to differential behavior tuples, bug signatures, and status classes

## Helpful methods

- `scheduler.add(seed)`
- `scheduler.next()`
- `scheduler.empty()`
- `scheduler.stats()`
- `scheduler.debug_dump(limit=20)` (inspect current scheduler contents)
- `ucb_tree.update(item, isinteresting_score=..., signals=...)`
- `thompson.update(item, isinteresting_score=..., signals=...)`

## Inspect current scheduler contents (debug)

Use `debug_dump()` to see what is currently inside the scheduler.

```python
print(scheduler.debug_dump(limit=10))
```

What it returns depends on the backend:

- `queue`: current queue order (`item_id`, `seed_id`, bucket, stats)
- `heap`: current priority order
- `ucb_tree`: leaf buckets (`coverage_key`, `bug_key`, leaf `N/Q`, seed IDs)

## Demo

```bash
python3 -m seed_scheduler.demo
```
