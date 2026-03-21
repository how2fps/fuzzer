from __future__ import annotations

import queue
import random
import sqlite3
import threading
import time
from multiprocessing import Process, Queue
from pathlib import Path
from typing import Any, Callable

from core.config import FuzzConfig
from core.db_utils import insert_run, insert_seen_branches, seed_stats_for_power_schedule
from core.llm_seed_fallback import make_generated_seed, maybe_generate_seed_candidates
from isinteresting import get_compute_interestingness
from core.mutation_utils import generate_unique_mutations, make_discovered_seed
from parser import get_parser
from core.paths import DISCOVERED_SEED_ORDINAL_BASE
from seed_scheduler import (
    BaseSeedScheduler,
    ScheduledSeed,
    build_ucb_update_signals,
)


def run_worker_process(
    config: FuzzConfig,
    request_queue: Queue,
    reply_queue: Queue,
    result_queue: Queue,
    worker_id: int,
    results_folder_str: str,
    effective_mutator: str,
) -> None:
    parser_api = get_parser(config["parser_version"])
    compute_interestingness_fn = get_compute_interestingness(
        config["isinteresting_version"]
    )
    effective_target = config["target"]
    results_folder = Path(results_folder_str)

    while True:
        # Ask for work from the coordinator. This call can be interrupted by
        # signals on some platforms, so let the parent decide when to stop
        # by sending a None work item.
        try:
            request_queue.put(1)
            work = reply_queue.get()
        except InterruptedError:
            # Allow the main process to handle shutdown; just retry.
            continue

        if work is None:
            break

        job_id = work["job_id"]
        item_id = work["item_id"]
        iteration = work["iteration"]
        seed_id = work["seed_id"]
        seed_text = work["seed_text"]
        seed_family = work.get("seed_family")
        bucket = work["bucket"]
        mutated_text = work["mutated_text"]

        try:
            result = parser_api.run_parser(
                input_data=mutated_text.encode("utf-8"),
                target=effective_target,
                timeout=config["timeout"],
                print_json=False,
                seed_family=seed_family,
            )
        except KeyboardInterrupt:
            # Treat a KeyboardInterrupt inside a worker as a request to stop
            # processing further work; break out of the loop so the process can
            # be joined or terminated by the parent.
            break

        print(result)
        db_path = results_folder / "runs.db"

        score = compute_interestingness_fn(
            result=result,
            db_path=db_path,
            target=work.get("target", ""),
        )
        closed = result.get("closed_result", {})
        signals = build_ucb_update_signals(
            result=result,
            db_path=db_path,
            target=work.get("target", ""),
            bucket=bucket,
            iteration=iteration,
            seed_id=seed_id,
            score=score,
        )
        insert_seen_branches(db_path, result)
        result_queue.put(
            {
                "job_id": job_id,
                "item_id": item_id,
                "iteration": iteration,
                "seed_id": seed_id,
                "seed_text": seed_text,
                "mutated_input": mutated_text,
                "status": closed.get("status"),
                "bug_signature": closed.get("bug_signature"),
                "isinteresting_score": score,
                "signals": signals,
            }
        )


def run_fuzzer_multi_worker(
    *,
    config: FuzzConfig,
    scheduler: BaseSeedScheduler,
    seed_energies: dict[int, int],
    corpus: Any,
    power_scheduler_module: Any,
    effective_target: str,
    effective_mutator: str,
    results_folder: Path,
    db_path: Path,
    conn: sqlite3.Connection,
    workers: int,
    shutdown_requested: list[bool],
    mutate_fn: Callable[..., str],
    rng: random.Random,
) -> None:
    scheduler_uses_feedback = scheduler.supports_feedback_updates()
    request_queue: Queue = Queue()
    reply_queue: Queue = Queue()
    result_queue: Queue = Queue()
    lock = threading.Lock()
    cond = threading.Condition(lock)
    total_jobs: list[int] = [0]
    pending: dict[int, tuple[ScheduledSeed, int]] = {}
    max_hours = config.get("max_hours")
    start_time = time.time()
    remaining_budget: list[int] | None = (
        [config["max_iterations"]] if config["max_iterations"] is not None else None
    )
    current_scheduled: list[ScheduledSeed | None] = [None]
    current_mutations_left: list[int] = [0]
    current_batch: list[str] = []
    job_id_counter: list[int] = [0]
    iteration_counter: list[int] = [0]
    seed_energies_holder: list[dict[int, int]] = [seed_energies]
    batch_expected: dict[str, int] = {}
    family = corpus.target(effective_target).family
    added_seed_inputs_holder: list[set[str]] = [set()]
    next_discovered_ordinal_holder: list[int] = [DISCOVERED_SEED_ORDINAL_BASE]
    results_received_count: list[int] = [0]
    consecutive_non_novel_results: list[int] = [0]
    llm_refill_attempts: list[int] = [0]

    def _try_refill_scheduler_from_llm() -> bool:
        if not config.get("llm_seed_fallback"):
            return False
        conn_thread = sqlite3.connect(str(db_path))
        try:
            generated = maybe_generate_seed_candidates(
                conn=conn_thread,
                corpus=corpus,
                target=effective_target,
                config=config,
                results_folder=results_folder,
            )
        finally:
            conn_thread.close()
        if generated is None or not generated.seeds:
            return False

        added_any = False
        for text in generated.seeds:
            if text in added_seed_inputs_holder[0]:
                continue
            candidate = make_generated_seed(
                text=text,
                family=family,
                ordinal=next_discovered_ordinal_holder[0],
            )
            candidate_metadata: dict[str, Any] = {
                "bucket": candidate.bucket,
                "signals": {
                    "coverage_key": {"family": candidate.family, "bucket": candidate.bucket},
                    "status": "generated",
                },
            }
            scheduler.add(candidate, metadata=candidate_metadata)
            added_seed_inputs_holder[0].add(text)
            next_discovered_ordinal_holder[0] += 1
            added_any = True
        if added_any:
            llm_refill_attempts[0] += 1
            print(
                f"LLM seed fallback added {len(generated.seeds)} candidate seeds "
                f"(attempt {llm_refill_attempts[0]})."
            )
        return added_any

    def request_handler() -> None:
        nones_sent = 0
        while nones_sent < workers:
            try:
                request_queue.get()
            except InterruptedError:
                # System call interrupted by signal; re-check shutdown flag.
                if shutdown_requested[0]:
                    with cond:
                        if total_jobs[0] == 0:
                            total_jobs[0] = iteration_counter[0]
                        # Send termination sentinels to any remaining workers.
                        remaining = workers - nones_sent
                        for _ in range(remaining):
                            reply_queue.put(None)
                        nones_sent = workers
                    break
                continue

            with cond:
                while True:
                    if shutdown_requested[0]:
                        if total_jobs[0] == 0:
                            total_jobs[0] = iteration_counter[0]
                        reply_queue.put(None)
                        nones_sent += 1
                        break
                    time_limit_exceeded = (
                        max_hours is not None
                        and (time.time() - start_time) >= max_hours * 3600
                    )
                    if time_limit_exceeded or (
                        remaining_budget is not None and remaining_budget[0] <= 0
                    ):
                        if total_jobs[0] == 0:
                            total_jobs[0] = iteration_counter[0]
                        reply_queue.put(None)
                        nones_sent += 1
                        break
                    if not scheduler.empty():
                        if current_mutations_left[0] <= 0:
                            conn_thread = sqlite3.connect(str(db_path))
                            try:
                                stats = seed_stats_for_power_schedule(
                                    corpus=corpus,
                                    target=effective_target,
                                    conn=conn_thread,
                                )
                                if stats:
                                    schedule = (
                                        power_scheduler_module.compute_power_schedule(
                                            seeds=stats
                                        )
                                    )
                                    seed_energies_holder[0] = dict(
                                        schedule["seed_energies"]
                                    )
                                current_scheduled[0] = scheduler.next()

                                energy = (
                                    1
                                    if scheduler_uses_feedback
                                    else seed_energies_holder[0].get(
                                        current_scheduled[0].seed.ordinal, 1
                                    )
                                )

                                n = (
                                    min(max(1, energy), remaining_budget[0])
                                    if remaining_budget is not None
                                    else max(1, energy)
                                )
                                current_batch.clear()
                                current_batch.extend(
                                    generate_unique_mutations(
                                        n,
                                        current_scheduled[0].seed.text,
                                        mutate_fn,
                                        effective_mutator,
                                        rng,
                                        conn_thread,
                                        effective_target,
                                    )
                                )
                                current_mutations_left[0] = len(current_batch)
                                batch_expected[current_scheduled[0].item_id] = len(
                                    current_batch
                                )
                                mode = (
                                    "single-mutation bandit"
                                    if scheduler_uses_feedback
                                    else "batch"
                                )
                                print(
                                  f"Scheduled seed {current_scheduled[0].seed.seed_id} with energy {energy} ({len(current_batch)} unique mutations, mode={mode})"
                                )
                            finally:
                                conn_thread.close()
                        scheduled = current_scheduled[0]
                        current_mutations_left[0] -= 1
                        if remaining_budget is not None:
                            remaining_budget[0] -= 1
                        job_id_counter[0] += 1
                        iteration_counter[0] += 1
                        job_id = job_id_counter[0]
                        iteration = iteration_counter[0] - 1
                        mutated_text = current_batch.pop(0)
                        work = {
                            "job_id": job_id,
                            "item_id": scheduled.item_id,
                            "iteration": iteration,
                            "seed_id": scheduled.seed.seed_id,
                            "seed_text": scheduled.seed.text,
                            "seed_family": scheduled.seed.family,
                            "bucket": scheduled.seed.bucket,
                            "target": effective_target,
                            "mutated_text": mutated_text,
                        }
                        pending[job_id] = (scheduled, iteration)
                        reply_queue.put(work)
                        break
                    while (
                        scheduler.empty()
                        and results_received_count[0] < iteration_counter[0]
                    ):
                        cond.wait()
                    if not scheduler.empty():
                        continue
                    if _try_refill_scheduler_from_llm():
                        cond.notify_all()
                        continue
                    if total_jobs[0] == 0:
                        total_jobs[0] = iteration_counter[0]
                    reply_queue.put(None)
                    nones_sent += 1
                    break

    request_thread = threading.Thread(target=request_handler)
    request_thread.start()

    procs = [
        Process(
            target=run_worker_process,
            args=(
                config,
                request_queue,
                reply_queue,
                result_queue,
                w,
                str(results_folder),
                effective_mutator,
            ),
        )
        for w in range(workers)
    ]
    for p in procs:
        p.start()

    results_received = 0
    batch_scores_by_item: dict[str, list[float]] = {}
    try:
        while True:
            try:
                # Use a timeout so we can periodically check for shutdown
                # requests even when no new results are arriving.
                result = result_queue.get(timeout=0.5)
            except queue.Empty:
                if shutdown_requested[0]:
                    break
                continue
            except InterruptedError:
                # System call interrupted by signal; if a shutdown has been
                # requested, stop waiting for more results.
                if shutdown_requested[0]:
                    break
                continue

            with cond:
                job_id = result["job_id"]
                scheduled, iteration = pending.pop(job_id, (None, None))
                if scheduled is None:
                    # Should not happen, but avoid crashing on shutdown races.
                    continue
                item_id = scheduled.item_id
                score = result["isinteresting_score"]
                if scheduler_uses_feedback:
                    scheduler.update(
                        scheduled,
                        isinteresting_score=score,
                        signals=result["signals"],
                    )
                else:
                    batch_scores_by_item.setdefault(item_id, []).append(score)
                    expected = batch_expected.get(item_id, 1)
                    if len(batch_scores_by_item[item_id]) >= expected:
                        batch_scores_by_item.pop(item_id, [])
                        batch_expected.pop(item_id, None)
            insert_run(
                conn,
                iteration=iteration,
                seed_id=result["seed_id"],
                seed_text=result["seed_text"],
                mutated_input=result["mutated_input"],
                status=result["status"],
                bug_signature=result["bug_signature"],
                isinteresting_score=result["isinteresting_score"],
                target=effective_target,
            )
            with cond:
                if (
                    result["isinteresting_score"] > 0
                    and result["mutated_input"] not in added_seed_inputs_holder[0]
                ):
                    parent_signals = (result.get("signals") or {}).copy()
                    parent_bucket = parent_signals.get("bucket", "discovered")
                    candidate = make_discovered_seed(
                        result["mutated_input"],
                        family,
                        parent_bucket,
                        next_discovered_ordinal_holder[0],
                    )
                    candidate_metadata: dict[str, Any] = {
                        "bucket": candidate.bucket,
                        "parent_seed_id": result["seed_id"],
                    }
                    if parent_signals:
                        candidate_metadata["signals"] = parent_signals
                    if scheduler_uses_feedback and config["ucb_trace"]:
                        candidate_metadata["_ucb_trace"] = True
                    scheduler.add(candidate, metadata=candidate_metadata)
                    added_seed_inputs_holder[0].add(result["mutated_input"])
                    next_discovered_ordinal_holder[0] += 1
                    cond.notify()
            if any(
                bool((result.get("signals") or {}).get(key))
                for key in ("new_coverage", "new_bug", "new_differential_behavior")
            ):
                consecutive_non_novel_results[0] = 0
            else:
                consecutive_non_novel_results[0] += 1
            if (
                config.get("llm_seed_fallback")
                and config.get("llm_seed_stagnation_threshold", 0) > 0
                and consecutive_non_novel_results[0]
                >= config["llm_seed_stagnation_threshold"]
            ):
                with cond:
                    if _try_refill_scheduler_from_llm():
                        consecutive_non_novel_results[0] = 0
                        cond.notify_all()
            results_received += 1
            with cond:
                results_received_count[0] = results_received
                cond.notify()
            if iteration:
                print(
                    f"[iter {iteration}] seed={result['seed_id']} "
                    f"score={result['isinteresting_score']:.3f} status={result['status']} input={result['seed_text']} mutated input={result['mutated_input']}"
                )
            if scheduler_uses_feedback and config["ucb_debug_tree"]:
                print(scheduler.render_tree(limit=12))
            if total_jobs[0] > 0 and results_received >= total_jobs[0]:
                break
            if shutdown_requested[0]:
                # On explicit shutdown (Ctrl+C), stop waiting for any
                # remaining in-flight work; we'll tear down workers below.
                break
    finally:
        request_thread.join(timeout=1.0)
        for p in procs:
            if p.is_alive():
                p.terminate()
        for p in procs:
            p.join()
