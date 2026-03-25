# Multi-Worker Flow

This note explains the multi-worker path in [`main.py`](/Users/joshuapacheco/projects/testing/fuzzer/main.py).

## Threads vs Processes

- A `thread` runs inside one process and shares that process's memory.
- A `process` is a separate Python runtime with its own memory.

In this fuzzer:

- The main process owns the scheduler, counters, current batch, and database coordination.
- The request thread runs inside the main process, so it can access that shared state directly.
- Worker processes do not share those Python variables. They only communicate through `multiprocessing.Queue`.

## The Three Queues

- `request_queue`: workers say "I am free, give me work".
- `reply_queue`: the request thread sends one work item back to a worker.
- `result_queue`: a worker sends its completed result back to the main thread.

## Terms

- `ScheduledSeed`: the object returned by `scheduler.next()`. It is the scheduler's chosen seed entry plus metadata like `item_id`.
- `energy`: how many mutated inputs to generate from that `ScheduledSeed`.
- `work item`: one concrete parser run for one mutated input from that scheduled seed.
- `result`: the outcome of one work item.

## High-Level Layout

```text
Main process
  - Main thread: receives results, updates scheduler, writes DB, adds discovered seeds
  - Request thread: waits for idle workers, chooses the next scheduled seed, sends work items

Worker processes
  - Run parser on one mutated input
  - Score the result
  - Send result back
```

## Worker Lifecycle

The worker loop is:

```python
while True:
    request_queue.put(1)
    work = reply_queue.get()
    if work is None:
        break
    ... run one work item ...
    result_queue.put(result)
```

Important point:

- "Idle" is not stored anywhere as a special state.
- A worker becomes idle simply by finishing its current job and reaching the top of the loop again.
- At that point it does `request_queue.put(1)`, which means "I am ready for more work".

So the worker does not pull jobs directly. It first announces availability.

## Request Thread Lifecycle

The request thread does this:

1. Wait for a worker request from `request_queue`.
2. Check stop conditions like shutdown, time limit, or iteration budget.
3. If needed, compute a fresh power schedule.
4. Pick the next `ScheduledSeed` from the scheduler.
5. Use that scheduled seed's energy to generate a batch of unique mutations.
6. Pop one mutation from the batch.
7. Send one work item through `reply_queue`.

The request thread is a dispatcher. It does not run the parser.

## Main Thread Lifecycle

The main thread does this:

1. Wait for a completed result from `result_queue`.
2. Match the result back to the original `ScheduledSeed` using `job_id`.
3. Update the scheduler with the observed score/signals.
4. Write the run to the SQLite database.
5. If the input is interesting, add it back as a discovered seed.
6. Notify the request thread that new work may now exist.

## Why Use Both a Thread and Processes

The main process has two blocking jobs:

- wait for worker requests
- wait for worker results

If one loop handled both, it would block on one side and delay the other.

So the design is:

- request thread handles incoming worker requests
- main thread handles incoming worker results
- worker processes do the expensive parser execution in parallel

## End-to-End Flow

```text
1. Worker finishes a work item
2. Worker -> request_queue: "ready"
3. Request thread receives that request
4. Request thread chooses a `ScheduledSeed` and one mutated input from its batch
5. Request thread -> reply_queue: work item
6. Worker receives work
7. Worker runs parser and computes score
8. Worker -> result_queue: result
9. Main thread receives result
10. Main thread updates scheduler and DB
11. If interesting, main thread adds a discovered seed
12. Repeat
```

## Energy / Batch Model

For one scheduler selection:

1. `scheduler.next()` returns one `ScheduledSeed`.
2. The power scheduler returns an energy for that seed.
3. That energy becomes the batch size.
4. The request thread generates that many unique mutations from the seed text.
5. Each mutation becomes a separate work item.
6. Workers consume those work items one at a time as they become free.

So if energy is `5`, that means:

- choose 1 `ScheduledSeed`
- generate 5 mutated inputs from it
- create 5 separate work items
- hand those 5 work items out across workers as they ask for work

It does not mean one worker is reserved for that seed.

## Mental Model

Think of it this way:

- Scheduler: chooses which seed matters next.
- Request thread: turns one scheduled seed into work items and dispatches them to free workers.
- Worker processes: execute one parser run for one work item.
- Main thread: learns from results and updates future scheduling.
