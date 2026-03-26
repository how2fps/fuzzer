from __future__ import annotations

import queue
import json
import random
import sqlite3
import sys
import threading
import time
from contextlib import nullcontext
from multiprocessing import Process, Queue
from pathlib import Path
from typing import Any, Callable, Sequence

from tqdm import tqdm

from core.config import FuzzConfig, is_debug_run
from core.fuzzer_logging import get_fuzzer_logger
from core.live_ui import RunDashboard, console
from core.db_utils import (
    insert_run,
    insert_seen_branches_into_conn,
    seed_stats_for_power_schedule,
)
from core.sqlite_conn import open_results_db
from core.llm_seed_fallback import make_generated_seed, maybe_generate_seed_candidates
from isinteresting import get_compute_interestingness
from core.mutation_utils import generate_unique_mutations, make_discovered_seed
from mutator import record_operator_coverage
from parser import get_parser
from core.paths import DISCOVERED_SEED_ORDINAL_BASE
from rich.live import Live
from seed_scheduler import (
    BaseSeedScheduler,
    ScheduledSeed,
    build_ucb_update_signals,
)


def run_worker_process(
    config: FuzzConfig,
    request_queue: Queue,
    reply_queues: Sequence[Queue],
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
    worker_reply: Queue = reply_queues[worker_id]
    wlog = get_fuzzer_logger()
    db_path = results_folder / "runs.db"
    db_conn = open_results_db(db_path)

    try:
        while True:
            # Ask for work from the coordinator. This call can be interrupted by
            # signals on some platforms, so let the parent decide when to stop
            # by sending a None work item.
            #
            # Each worker has its own reply queue so a work item cannot be taken
            # by a different worker (a bug when all workers shared one Queue).
            try:
                request_queue.put(worker_id)
                work = worker_reply.get()
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
                    enable_open_coverage=config.get("enable_open_coverage", False),
                    parser_config=config.get("parser_config"),  # type: ignore[arg-type]
                    closed_cwd_override=results_folder
                    / ".worker_cwd"
                    / f"w{worker_id}",
                )
            except KeyboardInterrupt:
                # Treat a KeyboardInterrupt inside a worker as a request to stop
                # processing further work; break out of the loop so the process can
                # be joined or terminated by the parent.
                break
            except Exception as exc:
                wlog.exception(
                    "Worker w%s: run_parser crashed (job_id=%s): %s",
                    worker_id,
                    job_id,
                    exc,
                )
                result = {
                    "closed_result": {
                        "status": "error",
                        "bug_signature": {
                            "type": type(exc).__name__,
                            "message": str(exc),
                        },
                    }
                }

            score = compute_interestingness_fn(
                result=result,
                db_path=db_path,
                target=work.get("target", ""),
                sqlite_conn=db_conn,
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
                sqlite_conn=db_conn,
            )
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
                    "parser_result": result,
                }
            )
    finally:
        db_conn.close()


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
    startup_generated_seeds: list[str] | None = None,
    startup_generated_source: str = "",
) -> None:
    scheduler_uses_feedback = scheduler.supports_feedback_updates()
    request_queue: Queue = Queue()
    reply_queues: list[Queue] = [Queue() for _ in range(workers)]
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
    family = corpus.resolve_family_or_target(effective_target)
    added_seed_inputs_holder: list[set[str]] = [set()]
    next_discovered_ordinal_holder: list[int] = [DISCOVERED_SEED_ORDINAL_BASE]
    results_received_count: list[int] = [0]
    llm_refill_attempts: list[int] = [0]
    last_low_value_signature: list[tuple[str, ...] | None] = [None]
    worker_shutdown_grace_seconds = max(5.0, float(config["timeout"]) * 2.0)
    log = get_fuzzer_logger()
    max_iterations = config["max_iterations"]
    debug_mode = is_debug_run(config)
    use_live_ui = not debug_mode
    seen_bug_keys: set[tuple[str, str, str, str, str]] = set()
    dashboard = RunDashboard(
        target=effective_target,
        workers=workers,
        results_folder=str(results_folder),
        max_iterations=max_iterations,
        max_hours=max_hours,
    )
    if use_live_ui and startup_generated_seeds:
        dashboard.finish_llm_generation(
            source=startup_generated_source or "startup bootstrap",
            seeds=startup_generated_seeds,
        )

    def _low_value_ready_signature(*, threshold: float = 0.1) -> tuple[str, ...] | None:
        ready_items = scheduler.ready_items()
        if not ready_items:
            return None
        if any(item.updates <= 0 for item in ready_items):
            return None
        if any(item.avg_isinteresting_score > threshold for item in ready_items):
            return None
        return tuple(sorted(item.seed.seed_id for item in ready_items))

    def _bug_key(result: dict[str, Any]) -> tuple[str, str, str, str, str] | None:
        status = str(result.get("status") or "").strip().lower()
        if status not in {"bug", "crash", "timeout", "error"}:
            return None
        signature = result.get("bug_signature") or {}
        if not isinstance(signature, dict):
            signature = {}
        return (
            status,
            str(signature.get("type") or ""),
            str(signature.get("exception") or ""),
            str(signature.get("file") or ""),
            str(signature.get("line") or ""),
        )

    def _try_refill_scheduler_from_llm() -> bool:
        requested = int(config["llm_seed_candidates"])
        if requested <= 0:
            return False
        if use_live_ui:
            dashboard.start_llm_generation(
                source="runtime refill",
                requested=requested,
            )
        conn_thread = open_results_db(db_path)
        try:
            generated = maybe_generate_seed_candidates(
                conn=conn_thread,
                corpus=corpus,
                target=effective_target,
                config=config,
                results_folder=results_folder,
                include_corpus_context=True,
            )
        finally:
            conn_thread.close()
        if generated is None or not generated.seeds:
            if use_live_ui:
                dashboard.fail_llm_generation(
                    source="runtime refill",
                    event="runtime refill returned no LLM seeds",
                )
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
            last_low_value_signature[0] = None
            log.info(
                "LLM seed fallback added %s candidate seeds (attempt %s).",
                len(generated.seeds),
                llm_refill_attempts[0],
            )
            if use_live_ui:
                dashboard.finish_llm_generation(
                    source="runtime refill",
                    seeds=generated.seeds,
                )
        elif use_live_ui:
            dashboard.fail_llm_generation(
                source="runtime refill",
                event="runtime refill produced only duplicate LLM seeds",
            )
        return added_any

    def request_handler() -> None:
        nones_sent = 0
        try:
            while nones_sent < workers:
                try:
                    wid = request_queue.get(timeout=1.0)
                    if not isinstance(wid, int) or not (0 <= wid < workers):
                        log.error(
                            "Invalid worker id %r (expected 0..%s); ignoring request",
                            wid,
                            workers - 1,
                        )
                        continue
                except queue.Empty:
                    stop_all_workers = False
                    with cond:
                        time_limit_exceeded = (
                            max_hours is not None
                            and (time.time() - start_time) >= max_hours * 3600
                        )
                        if shutdown_requested[0] or time_limit_exceeded or (
                            remaining_budget is not None
                            and remaining_budget[0] <= 0
                        ):
                            if total_jobs[0] == 0:
                                total_jobs[0] = iteration_counter[0]
                            for q in reply_queues:
                                q.put(None)
                            nones_sent = workers
                            stop_all_workers = True
                    if stop_all_workers:
                        break
                    continue
                except InterruptedError:
                    # System call interrupted by signal; re-check shutdown flag.
                    if shutdown_requested[0]:
                        with cond:
                            if total_jobs[0] == 0:
                                total_jobs[0] = iteration_counter[0]
                            for q in reply_queues:
                                q.put(None)
                            nones_sent = workers
                        break
                    continue

                with cond:
                    while True:
                        stop_coordinator_loop = False
                        if shutdown_requested[0]:
                            if total_jobs[0] == 0:
                                total_jobs[0] = iteration_counter[0]
                            reply_queues[wid].put(None)
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
                            reply_queues[wid].put(None)
                            nones_sent += 1
                            break
                        if not scheduler.empty():
                            low_value_signature = _low_value_ready_signature()
                            if (
                                low_value_signature is not None
                                and low_value_signature != last_low_value_signature[0]
                            ):
                                last_low_value_signature[0] = low_value_signature
                                if _try_refill_scheduler_from_llm():
                                    cond.notify_all()
                            if current_mutations_left[0] <= 0:
                                conn_thread = open_results_db(db_path)
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
                                    # Keep at least one mutation per in-flight worker when the power
                                    # schedule assigns low energy, so we do not drain the scheduler's
                                    # ready set (heap/queue) with one next() per worker before results
                                    # arrive and starve the rest.
                                    if not scheduler_uses_feedback and workers > 1:
                                        parallel_floor = (
                                            min(workers, remaining_budget[0])
                                            if remaining_budget is not None
                                            else workers
                                        )
                                        if parallel_floor >= 1:
                                            n = max(n, parallel_floor)
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
                                    if debug_mode:
                                        log.info(
                                            "Scheduled seed %s with energy %s (%s unique mutations, mode=%s)",
                                            current_scheduled[0].seed.seed_id,
                                            energy,
                                            len(current_batch),
                                            mode,
                                        )
                                    if use_live_ui:
                                        dashboard.record_schedule(
                                            pending_jobs=len(pending),
                                            queue_depth=len(batch_expected) + len(pending),
                                            event=(
                                                f"seed {current_scheduled[0].seed.seed_id} "
                                                f"scheduled with {len(current_batch)} mutations"
                                            ),
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
                            reply_queues[wid].put(work)
                            break
                        while (
                            scheduler.empty()
                            and results_received_count[0] < iteration_counter[0]
                        ):
                            if shutdown_requested[0]:
                                if total_jobs[0] == 0:
                                    total_jobs[0] = iteration_counter[0]
                                reply_queues[wid].put(None)
                                nones_sent += 1
                                stop_coordinator_loop = True
                                break
                            time_limit_exceeded = (
                                max_hours is not None
                                and (time.time() - start_time) >= max_hours * 3600
                            )
                            if time_limit_exceeded or (
                                remaining_budget is not None
                                and remaining_budget[0] <= 0
                            ):
                                if total_jobs[0] == 0:
                                    total_jobs[0] = iteration_counter[0]
                                reply_queues[wid].put(None)
                                nones_sent += 1
                                stop_coordinator_loop = True
                                break
                            cond.wait(timeout=0.5)
                        if stop_coordinator_loop:
                            break
                        if not scheduler.empty():
                            continue
                        if _try_refill_scheduler_from_llm():
                            cond.notify_all()
                            continue
                        if total_jobs[0] == 0:
                            total_jobs[0] = iteration_counter[0]
                        reply_queues[wid].put(None)
                        nones_sent += 1
                        break
        finally:
            with cond:
                if total_jobs[0] == 0 and iteration_counter[0] > 0:
                    total_jobs[0] = iteration_counter[0]

    request_thread = threading.Thread(target=request_handler)
    request_thread.start()

    procs = [
        Process(
            target=run_worker_process,
            args=(
                config,
                request_queue,
                reply_queues,
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
    last_result_time = time.time()
    batch_scores_by_item: dict[str, list[float]] = {}
    try:
        display_context = (
            Live(
                dashboard.render(),
                console=console,
                screen=True,
                refresh_per_second=4,
                transient=False,
                redirect_stdout=False,
                redirect_stderr=False,
                vertical_overflow="crop",
            )
            if use_live_ui
            else nullcontext()
        )
        with display_context as live:
            pbar = None if use_live_ui else tqdm(
                total=max_iterations,
                unit="iter",
                desc="Fuzz",
                dynamic_ncols=True,
                file=sys.stderr,
                mininterval=0.25,
            )
            while True:
                try:
                    # Use a timeout so we can periodically check for shutdown
                    # requests even when no new results are arriving.
                    result = result_queue.get(timeout=0.5)
                except queue.Empty:
                    time_limit_exceeded = (
                        max_hours is not None
                        and (time.time() - start_time) >= max_hours * 3600
                    )
                    iteration_budget_done = (
                        remaining_budget is not None
                        and remaining_budget[0] <= 0
                    )
                    if time_limit_exceeded or iteration_budget_done:
                        with cond:
                            if total_jobs[0] == 0 and iteration_counter[0] > 0:
                                total_jobs[0] = iteration_counter[0]
                        if total_jobs[0] > 0 and results_received >= total_jobs[0]:
                            break
                        idle = time.time() - last_result_time
                        if results_received > 0 and idle >= 60.0:
                            log.warning(
                                "Time or iteration budget reached but no new results for %.1fs "
                                "(%s received, job cap %s); forcing shutdown.",
                                idle,
                                results_received,
                                total_jobs[0] or iteration_counter[0],
                            )
                            if use_live_ui:
                                dashboard.update_status(
                                    "STOPPING",
                                    event="forcing shutdown after idle timeout",
                                )
                                live.update(dashboard.render())
                            break
                    if (
                        total_jobs[0] > 0
                        and results_received < total_jobs[0]
                        and (time.time() - last_result_time)
                        >= worker_shutdown_grace_seconds
                    ):
                        log.warning(
                            "Timed out waiting for remaining worker results; forcing shutdown."
                        )
                        if use_live_ui:
                            dashboard.update_status(
                                "STOPPING", event="timed out waiting for workers"
                            )
                            live.update(dashboard.render())
                        break
                    if shutdown_requested[0]:
                        if use_live_ui:
                            dashboard.update_status(
                                "STOPPING", event="shutdown requested"
                            )
                            live.update(dashboard.render())
                        break
                    if use_live_ui:
                        live.update(dashboard.render())
                    continue
                except InterruptedError:
                    # System call interrupted by signal; if a shutdown has been
                    # requested, stop waiting for more results.
                    if shutdown_requested[0]:
                        if use_live_ui:
                            dashboard.update_status(
                                "STOPPING", event="shutdown requested"
                            )
                            live.update(dashboard.render())
                        break
                    if use_live_ui:
                        live.update(dashboard.render())
                    continue

                with cond:
                    job_id = result["job_id"]
                    scheduled, iteration = pending.pop(job_id, (None, None))
                    if scheduled is None:
                        iteration = result.get("iteration")
                        if iteration is None:
                            log.warning(
                                "Dropping result with no pending job_id=%s and no iteration field",
                                job_id,
                            )
                            continue
                        log.warning(
                            "Orphan result (missing pending) job_id=%s; recording run anyway",
                            job_id,
                        )
                    else:
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
                                finished_scores = batch_scores_by_item.pop(item_id)
                                batch_expected.pop(item_id, None)
                                scheduler.complete_batch(
                                    scheduled, batch_scores=finished_scores
                                )
                                cond.notify_all()
                parser_result = result.pop("parser_result", None)
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
                if parser_result is not None:
                    insert_seen_branches_into_conn(conn, parser_result)
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
                            "initial_isinteresting_score": result["isinteresting_score"],
                        }
                        if parent_signals:
                            candidate_metadata["signals"] = parent_signals
                        if scheduler_uses_feedback and config["ucb_trace"]:
                            candidate_metadata["_ucb_trace"] = True
                        scheduler.add(candidate, metadata=candidate_metadata)
                        added_seed_inputs_holder[0].add(result["mutated_input"])
                        next_discovered_ordinal_holder[0] += 1
                        last_low_value_signature[0] = None
                        cond.notify()
                results_received += 1
                last_result_time = time.time()
                with cond:
                    last_low_value_signature[0] = None
                    results_received_count[0] = results_received
                    pending_jobs = len(pending)
                    queue_depth = len(batch_expected) + len(pending)
                    cond.notify()

                status = str(result.get("status") or "").strip().lower() or "unknown"
                signals = result.get("signals") or {}
                new_coverage = bool(signals.get("new_coverage"))
                new_bug = bool(signals.get("new_bug"))
                bug_key = _bug_key(result)
                if bug_key is not None and bug_key not in seen_bug_keys:
                    seen_bug_keys.add(bug_key)
                    new_bug = True

                # Feed the real coverage signal back to the adaptive operator strategy.
                record_operator_coverage(result["mutated_input"], new_coverage)

                event_bits = [f"iter {iteration}", status]
                if new_bug:
                    event_bits.append("new bug")
                if new_coverage:
                    event_bits.append("new coverage")
                if use_live_ui:
                    dashboard.record_result(
                        status=status,
                        score=result["isinteresting_score"],
                        new_coverage=new_coverage,
                        new_bug=new_bug,
                        pending_jobs=pending_jobs,
                        queue_depth=queue_depth,
                        event=" | ".join(event_bits),
                        mutated_input=result["mutated_input"],
                    )

                if debug_mode:
                    if parser_result is not None:
                        try:
                            log.info(
                                "%s",
                                json.dumps(parser_result, default=str, sort_keys=True),
                            )
                        except (TypeError, ValueError):
                            log.info("%s", repr(parser_result))
                    log.info(
                        "[iter %s] seed=%s score=%.3f status=%s input=%s mutated input=%s",
                        iteration,
                        result["seed_id"],
                        result["isinteresting_score"],
                        result["status"],
                        result["seed_text"],
                        result["mutated_input"],
                    )
                if debug_mode and new_bug:
                    log.info(
                        "Unique bug %s found at iteration %s (seed=%s).",
                        dashboard.unique_bugs_found if use_live_ui else len(seen_bug_keys),
                        iteration,
                        result["seed_id"],
                    )
                if scheduler_uses_feedback and config["ucb_debug_tree"]:
                    log.info("%s", scheduler.render_tree(limit=12))
                if use_live_ui:
                    live.update(dashboard.render())
                elif pbar is not None:
                    pbar.update(1)
                if total_jobs[0] > 0 and results_received >= total_jobs[0]:
                    if use_live_ui:
                        dashboard.update_status("DONE", event="run complete")
                        live.update(dashboard.render())
                    break
                if shutdown_requested[0]:
                    # On explicit shutdown (Ctrl+C), stop waiting for any
                    # remaining in-flight work; we'll tear down workers below.
                    if use_live_ui:
                        dashboard.update_status("STOPPING", event="shutdown requested")
                        live.update(dashboard.render())
                    break
            if pbar is not None:
                pbar.close()
    finally:
        request_thread.join(timeout=1.0)
        for p in procs:
            if p.is_alive():
                p.terminate()
        for p in procs:
            p.join(timeout=worker_shutdown_grace_seconds)
            if not p.is_alive():
                continue
            log.warning(
                "Worker process pid=%s did not exit after terminate(); killing.",
                p.pid,
            )
            p.kill()
            p.join(timeout=1.0)
