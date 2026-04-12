from __future__ import annotations

import queue
import json
import os
import random
import sqlite3
import sys
import threading
import time
from collections import deque
from contextlib import nullcontext
from multiprocessing import Process, Queue
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from coverage.parser import PythonParser
from coverage.exceptions import NoSource
from tqdm import tqdm

from core.config import FuzzConfig, is_debug_run
from core.fuzzer_logging import get_fuzzer_logger
from core.live_ui import RunDashboard, console
from core.db_utils import (
    add_unique_coverage_seed_input,
    add_seed_input_if_new,
    get_coverage_replay_seed_inputs,
    increment_edge_observations,
    input_already_run,
    insert_run,
    insert_seen_edges_into_conn,
    seed_stats_for_power_schedule,
)
from core.sqlite_conn import open_results_db
from core.llm_seed_fallback import make_generated_seed, maybe_generate_seed_candidates
from isinteresting import (
    get_compute_interestingness,
    get_covered_edges_from_result,
    get_coverage_source_kind_from_result,
)
from core.mutation_utils import make_discovered_seed
from core.seed_refill import collect_history_texts, generate_grammar_refill_seeds
from parser import get_parser
from core.paths import DISCOVERED_SEED_ORDINAL_BASE
from rich.live import Live
from seed_scheduler import (
    BaseSeedScheduler,
    ScheduledSeed,
    build_ucb_update_signals,
)


_BRANCH_ARC_CACHE: dict[str, set[tuple[int, int]]] = {}


def _bug_key_from_result(result: Mapping[str, Any]) -> tuple[str, str, str] | None:
    status = str(result.get("status") or "").strip().lower()
    if status not in {"bug", "crash", "timeout"}:
        return None
    signature = result.get("bug_signature") or {}
    if not isinstance(signature, Mapping):
        return None
    file_name = str(signature.get("file") or "").strip()
    bug_type = str(signature.get("type") or "").strip()
    line = str(signature.get("line") or "").strip()
    if not file_name or not bug_type or not line:
        return None
    return (file_name, bug_type, line)


def _load_seen_bug_keys(
    *,
    conn: sqlite3.Connection,
    target: str,
) -> set[tuple[str, str, str]]:
    rows = conn.execute(
        """
        SELECT DISTINCT file, bug_type, line
        FROM runs
        WHERE target = ?
          AND status IN ('bug', 'crash', 'timeout')
          AND file IS NOT NULL
          AND line IS NOT NULL
          AND COALESCE(bug_type, '') != ''
        """,
        (target,),
    ).fetchall()
    return {
        (str(file_name).strip(), str(bug_type).strip(), str(line).strip())
        for file_name, bug_type, line in rows
    }


def _claim_new_bug_flag(
    *,
    seen_bug_keys: set[tuple[str, str, str]],
    result: Mapping[str, Any],
) -> bool:
    bug_key = _bug_key_from_result(result)
    if bug_key is None or bug_key in seen_bug_keys:
        return False
    seen_bug_keys.add(bug_key)
    return True


def _build_execution_batch(
    *,
    item: ScheduledSeed,
    n: int,
    target: str,
    conn_thread: sqlite3.Connection,
    generate_mutation_batch: Callable[..., list[tuple[str, float]]],
) -> list[tuple[str, float]]:
    """Build one execution batch, prepending an unseen seed text before mutations."""
    if n <= 0:
        return []

    batch: list[tuple[str, float]] = []
    batch_texts: set[str] = set()
    already_consumed = bool(item.metadata.get("seed_unmutated_batch_consumed")) or bool(
        item.metadata.get("startup_generated_unmutated_consumed")
    )

    if not already_consumed:
        if not input_already_run(conn_thread, item.seed.text, target):
            batch.append((item.seed.text, 0.0))
            batch_texts.add(item.seed.text)
        item.metadata["seed_unmutated_batch_consumed"] = True
        if (
            "startup_generated_run_unmutated_first" in item.metadata
            or "startup_generated_unmutated_consumed" in item.metadata
        ):
            item.metadata["startup_generated_unmutated_consumed"] = True

    remaining = max(0, n - len(batch))
    if remaining > 0:
        batch.extend(
            generate_mutation_batch(
                n=remaining,
                seed_text=item.seed.text,
                conn_thread=conn_thread,
                reserved_inputs=tuple(batch_texts),
            )
        )

    return batch


def _lease_next_schedulable_item(
    *,
    startup_warm_queue: deque[ScheduledSeed],
    scheduler: BaseSeedScheduler,
) -> tuple[ScheduledSeed, bool]:
    """Lease the next item, preferring startup warm seeds before scheduler items."""
    if startup_warm_queue:
        return startup_warm_queue.popleft(), True
    return scheduler.next(), False


def _reinsert_startup_warm_seed(
    *,
    scheduler: BaseSeedScheduler,
    item: ScheduledSeed,
    isinteresting_score: float,
    signals: Mapping[str, Any] | None,
) -> ScheduledSeed:
    """
    Insert a startup warm seed into the scheduler after its first execution.

    The first run happens outside the scheduler so startup seeds cannot be
    evicted before they are exercised at least once.
    """
    metadata = dict(item.metadata)
    metadata.pop("startup_warm_pending_insert", None)
    metadata["initial_isinteresting_score"] = float(isinteresting_score)
    metadata["signals"] = dict(signals or metadata.get("signals") or {})
    inserted = scheduler.add(item.seed, metadata=metadata)
    if inserted.updates <= 0:
        inserted.updates = 1
        inserted.total_isinteresting_score = float(isinteresting_score)
        inserted.last_isinteresting_score = float(isinteresting_score)
    return inserted


def _clear_completed_feedback_batch(
    *,
    batch_expected: dict[str, int],
    item: ScheduledSeed,
) -> None:
    """
    Drop worker-side batch tracking once a feedback scheduler has re-queued the item.

    Feedback schedulers such as `ucb_tree` track their own remaining result count on
    the scheduled item metadata. The live UI uses `batch_expected` only to count
    leases that are still outstanding, so completed entries must be removed once the
    item becomes ready again.
    """
    try:
        remaining = int(item.metadata.get("_ucb_pending_batch_results", 0))
    except (TypeError, ValueError):
        remaining = 0
    if remaining <= 0:
        batch_expected.pop(item.item_id, None)


def _coverage_replay_ready_signature(
    ready_items: Sequence[ScheduledSeed],
    *,
    score_threshold: float = 0.1,
    hot_item_fraction_ceiling: float = 0.25,
) -> tuple[str, ...] | None:
    """
    Return a stable signature when the pool looks stale enough to justify replay.

    Unlike the old "any hot seed blocks refill" check, this tolerates a small
    number of still-promising seeds while allowing replay to broaden the pool.
    """
    if not ready_items:
        return None
    if any(item.updates <= 0 for item in ready_items):
        return None
    hot_items = sum(
        1
        for item in ready_items
        if item.avg_isinteresting_score > score_threshold
    )
    hot_ratio = hot_items / float(len(ready_items))
    if hot_ratio > hot_item_fraction_ceiling:
        return None
    return tuple(sorted(item.seed.seed_id for item in ready_items))


def _find_newest_unseen_branch(
    conn: sqlite3.Connection,
    covered_edges: Sequence[tuple[str, int, int]],
) -> str:
    """Return a compact description of the first branch in this result not yet seen."""
    for file_name, from_line, to_line in covered_edges:
        row = conn.execute(
            "SELECT 1 FROM seen_branches WHERE file = ? AND from_line = ? AND to_line = ? LIMIT 1",
            (file_name, from_line, to_line),
        ).fetchone()
        if row is not None:
            continue
        file_label = Path(file_name).name or str(file_name)
        target_label = "exit" if int(to_line) < 0 else str(to_line)
        return f"{file_label}:{from_line} -> {target_label}"
    return ""


def _count_seen_branches(conn: sqlite3.Connection) -> int:
    """Return the accumulated number of unique covered arcs recorded so far."""
    row = conn.execute("SELECT COUNT(*) FROM seen_branches").fetchone()
    if row is None:
        return 0
    try:
        return max(0, int(row[0]))
    except (TypeError, ValueError):
        return 0


def _branch_arcs_for_file(file_name: str) -> set[tuple[int, int]]:
    cached = _BRANCH_ARC_CACHE.get(file_name)
    if cached is not None:
        return cached

    path = Path(file_name)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path

    branch_arcs: set[tuple[int, int]] = set()
    try:
        parser = PythonParser(filename=str(path))
        parser.parse_source()
        arcs = parser.arcs() or []
    except (OSError, NoSource):
        _BRANCH_ARC_CACHE[file_name] = branch_arcs
        return branch_arcs

    exits: dict[int, set[int]] = {}
    for from_line, to_line in arcs:
        if from_line <= 0:
            continue
        exits.setdefault(from_line, set()).add(to_line)

    for from_line, targets in exits.items():
        if len(targets) <= 1:
            continue
        for to_line in targets:
            branch_arcs.add((from_line, to_line))

    _BRANCH_ARC_CACHE[file_name] = branch_arcs
    return branch_arcs


def _count_branch_like_arcs(arcs: set[tuple[int, int]]) -> int:
    exits: dict[int, set[int]] = {}
    for from_line, to_line in arcs:
        if from_line <= 0 or to_line <= 0:
            continue
        exits.setdefault(from_line, set()).add(to_line)
    total = 0
    for targets in exits.values():
        if len(targets) <= 1:
            continue
        total += len(targets)
    return total


def _count_seen_covered_branches(conn: sqlite3.Connection) -> int:
    """Return the accumulated number of unique covered branches, excluding non-branch arcs."""
    total = 0
    rows = conn.execute(
        "SELECT file, from_line, to_line FROM seen_branches"
    ).fetchall()
    seen_by_file: dict[str, set[tuple[int, int]]] = {}
    for file_name, from_line, to_line in rows:
        try:
            seen_by_file.setdefault(str(file_name), set()).add((int(from_line), int(to_line)))
        except (TypeError, ValueError):
            continue

    for file_name, arcs in seen_by_file.items():
        branch_arcs = _branch_arcs_for_file(file_name)
        if branch_arcs:
            total += len(arcs & branch_arcs)
        else:
            total += _count_branch_like_arcs(arcs)
    return total


def _extract_total_branches(result: Mapping[str, Any]) -> int:
    """Return total branches from the active coverage source when available."""
    selected = _select_coverage_source(result)
    total = _extract_total_branches_from_candidate(selected)
    if total > 0:
        return total

    for candidate in (
        result.get("closed_result"),
        result.get("shadow_result"),
        result.get("open_result"),
    ):
        total = _extract_total_branches_from_candidate(
            candidate if isinstance(candidate, Mapping) else None
        )
        if total > 0:
            return total
    return 0


def _extract_total_branches_from_candidate(candidate: Mapping[str, Any] | None) -> int:
    if not isinstance(candidate, Mapping):
        return 0
    details = candidate.get("branch_details_by_file")
    if not isinstance(details, Sequence):
        return 0
    explicit_total = candidate.get("total_branches")
    if explicit_total not in (None, ""):
        try:
            total = int(explicit_total)
        except (TypeError, ValueError):
            total = 0
        if total > 0:
            return total
    covered_raw = candidate.get("covered_branches")
    missing_raw = candidate.get("missing_branches")
    if covered_raw is None or missing_raw is None:
        return 0
    try:
        covered = int(covered_raw or 0)
        missing = int(missing_raw or 0)
    except (TypeError, ValueError):
        return 0
    total = covered + missing
    return total if total > 0 else 0


def _select_coverage_source(result: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(result, Mapping):
        return {}
    kind = get_coverage_source_kind_from_result(result)
    if kind == "shadow":
        candidate = result.get("shadow_result")
    elif kind == "open":
        candidate = result.get("open_result")
    else:
        candidate = result.get("closed_result")
    return candidate if isinstance(candidate, Mapping) else {}


def _extract_coverage_backend(result: Mapping[str, Any]) -> str:
    source = _select_coverage_source(result)
    return str(source.get("coverage_backend") or "").strip()


def _extract_covered_lines(result: Mapping[str, Any]) -> int:
    source = _select_coverage_source(result)
    raw = source.get("covered_lines")
    if raw in (None, ""):
        return 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def _extract_total_lines(result: Mapping[str, Any]) -> int:
    source = _select_coverage_source(result)
    raw = source.get("total_lines")
    if raw in (None, ""):
        return 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def _extract_total_edges(result: Mapping[str, Any]) -> int:
    source = _select_coverage_source(result)
    raw = source.get("total_edges")
    if raw in (None, ""):
        return 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def _read_rss_kib(pid: int) -> int | None:
    """Best-effort RSS lookup from /proc/<pid>/status in KiB."""
    status_path = Path(f"/proc/{pid}/status")
    try:
        with open(status_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.startswith("VmRSS:"):
                    continue
                parts = line.split()
                if len(parts) < 2:
                    return None
                try:
                    return int(parts[1])
                except ValueError:
                    return None
    except OSError:
        return None
    return None


def _format_rss(kib: int | None) -> str:
    if kib is None:
        return "n/a"
    return f"{(kib / 1024.0):.1f}MiB"


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
    debug_mode = is_debug_run(config)
    worker_max_jobs = max(0, int(config.get("worker_max_jobs") or 0))
    completed_jobs = 0

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
            generation_time_seconds = float(work.get("generation_time_seconds") or 0.0)

            try:
                run_started_at = time.perf_counter()
                result = parser_api.run_parser(
                    input_data=mutated_text.encode("utf-8"),
                    target=effective_target,
                    timeout=config["timeout"],
                    print_json=False,
                    seed_family=seed_family,
                    enable_open_coverage=config.get("enable_open_coverage", False),
                    enable_qemu_coverage=config.get("enable_qemu_coverage", False),
                    enable_pyc_coverage=config.get("enable_pyc_coverage", False),
                    parser_config=config.get("parser_config"),  # type: ignore[arg-type]
                    closed_cwd_override=results_folder
                    / ".worker_cwd"
                    / f"w{worker_id}",
                )
                run_time_seconds = time.perf_counter() - run_started_at
            except KeyboardInterrupt:
                # Treat a KeyboardInterrupt inside a worker as a request to stop
                # processing further work; break out of the loop so the process can
                # be joined or terminated by the parent.
                break
            except Exception as exc:
                run_time_seconds = time.perf_counter() - run_started_at
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
                input_text=mutated_text,
                sqlite_conn=db_conn,
            )
            closed = result.get("closed_result", {})
            coverage_source_kind = get_coverage_source_kind_from_result(result)
            coverage_backend = _extract_coverage_backend(result)
            covered_lines = _extract_covered_lines(result)
            total_lines = _extract_total_lines(result)
            total_edges = _extract_total_edges(result)
            signals = build_ucb_update_signals(
                result=result,
                db_path=db_path,
                target=work.get("target", ""),
                bucket=bucket,
                iteration=iteration,
                seed_id=seed_id,
                score=score,
                input_text=mutated_text,
                sqlite_conn=db_conn,
            )
            result_queue.put(
                {
                    "worker_id": worker_id,
                    "job_id": job_id,
                    "item_id": item_id,
                    "iteration": iteration,
                    "seed_id": seed_id,
                    "seed_text": seed_text,
                    "mutated_input": mutated_text,
                    "generation_time_seconds": generation_time_seconds,
                    "run_time_seconds": run_time_seconds,
                    "status": closed.get("status"),
                    "bug_signature": closed.get("bug_signature"),
                    "coverage_backend": coverage_backend,
                    "coverage_source_kind": coverage_source_kind,
                    "covered_lines": covered_lines,
                    "total_lines": total_lines,
                    "total_edges": total_edges,
                    "isinteresting_score": score,
                    "signals": signals,
                    "covered_edges": tuple(get_covered_edges_from_result(result)),
                    "total_branches": _extract_total_branches(result),
                    "parser_result": result if debug_mode else None,
                    "worker_retiring": (
                        worker_max_jobs > 0 and (completed_jobs + 1) >= worker_max_jobs
                    ),
                }
            )
            completed_jobs += 1
            if worker_max_jobs > 0 and completed_jobs >= worker_max_jobs:
                wlog.info(
                    "Worker w%s reached recycle threshold (%s jobs); exiting for refresh.",
                    worker_id,
                    worker_max_jobs,
                )
                break
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
    mutator_feedback_fn: Callable[..., bool] | None,
    rng: random.Random,
    startup_seed_items: Sequence[ScheduledSeed] | None = None,
    startup_generated_seeds: list[str] | None = None,
    startup_generated_source: str = "",
) -> None:
    scheduler_uses_feedback = scheduler.supports_feedback_updates()
    queue_cap = max(8, workers * 4)
    request_queue: Queue = Queue(maxsize=max(2, workers * 2))
    reply_queues: list[Queue] = [Queue(maxsize=1) for _ in range(workers)]
    result_queue: Queue = Queue(maxsize=queue_cap)
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
    current_batch: deque[tuple[str, float]] = deque()
    startup_warm_queue: deque[ScheduledSeed] = deque(startup_seed_items or ())
    job_id_counter: list[int] = [0]
    iteration_counter: list[int] = [0]
    seed_energies_holder: list[dict[int, int]] = [seed_energies]
    batch_expected: dict[str, int] = {}
    family = corpus.resolve_family_or_target(effective_target)
    next_discovered_ordinal_holder: list[int] = [DISCOVERED_SEED_ORDINAL_BASE]
    results_received_count: list[int] = [0]
    llm_refill_attempts: list[int] = [0]
    last_low_value_signature: list[tuple[str, ...] | None] = [None]
    worker_shutdown_grace_seconds = max(5.0, float(config["timeout"]) * 2.0)
    worker_max_jobs = max(0, int(config.get("worker_max_jobs") or 0))
    seed_refill_mode = str(config.get("seed_refill_mode") or "historical")
    log = get_fuzzer_logger()
    max_iterations = config["max_iterations"]
    debug_mode = is_debug_run(config)
    use_live_ui = not debug_mode
    seen_bug_keys = _load_seen_bug_keys(conn=conn, target=effective_target)
    dashboard = RunDashboard(
        target=effective_target,
        configured_workers=workers,
        results_folder=str(results_folder),
        max_iterations=max_iterations,
        max_hours=max_hours,
    )
    feedback_scheduler_batch_cap = max(
        1,
        int(config.get("feedback_scheduler_batch_cap") or 4),
    )
    if use_live_ui and startup_generated_seeds:
        dashboard.finish_llm_generation(
            source=startup_generated_source or "startup bootstrap",
            seeds=startup_generated_seeds,
        )

    def _ready_scheduler_size() -> int:
        return len(startup_warm_queue) + len(scheduler)

    def _generate_timed_mutation_batch(
        *,
        n: int,
        seed_text: str,
        conn_thread: sqlite3.Connection,
        reserved_inputs: Sequence[str] | None = None,
        max_attempts: int = 200,
    ) -> list[tuple[str, float]]:
        def _is_rejected_candidate(candidate: str) -> bool:
            return "\x00" in candidate

        seen: set[str] = set(reserved_inputs or ())
        batch: list[tuple[str, float]] = []
        for _ in range(n):
            started_at = time.perf_counter()
            candidate = mutate_fn(
                seed_text,
                mutator_kind=effective_mutator,
                rng=rng,
            )
            for _attempt in range(max_attempts):
                if _is_rejected_candidate(candidate):
                    candidate = mutate_fn(
                        seed_text,
                        mutator_kind=effective_mutator,
                        rng=rng,
                    )
                    continue
                if candidate not in seen and not input_already_run(
                    conn_thread, candidate, effective_target
                ):
                    seen.add(candidate)
                    batch.append((candidate, time.perf_counter() - started_at))
                    break
                candidate = mutate_fn(
                    seed_text,
                    mutator_kind=effective_mutator,
                    rng=rng,
                )
            else:
                seen.add(candidate)
                batch.append((candidate, time.perf_counter() - started_at))
        return batch

    def _coverage_key_from_signals(signals: dict[str, Any] | None) -> str | None:
        if not isinstance(signals, dict):
            return None
        coverage_key = signals.get("coverage_key")
        if coverage_key in (None, "", [], {}):
            return None
        return str(coverage_key)

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
            if generated is None or not generated.seeds:
                if use_live_ui:
                    dashboard.fail_llm_generation(
                        source="runtime refill",
                        event="runtime refill returned no LLM seeds",
                    )
                return False

            added_any = False
            for text in generated.seeds:
                if not add_seed_input_if_new(
                    conn_thread,
                    target=effective_target,
                    input_text=text,
                ):
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
        finally:
            conn_thread.close()

    def _try_refill_scheduler_from_unique_coverage_store() -> bool:
        refill_limit = max(1, int(config.get("seed_preload_total") or 8))
        replay_sample_limit = max(refill_limit, refill_limit * 4)
        conn_thread = open_results_db(db_path)
        try:
            stored = get_coverage_replay_seed_inputs(
                conn_thread,
                target=effective_target,
                rng=rng,
                limit=replay_sample_limit,
            )
        finally:
            conn_thread.close()
        if not stored:
            return False

        ready_texts = {item.seed.text for item in scheduler.ready_items()}
        added_count = 0
        for entry in stored:
            if added_count >= refill_limit:
                break
            text = entry.get("input_text", "")
            if not text or text in ready_texts:
                continue
            bucket = entry.get("seed_bucket") or "coverage_replay"
            candidate = make_discovered_seed(
                text,
                family,
                bucket,
                next_discovered_ordinal_holder[0],
            )
            coverage_key = entry.get("coverage_key")
            candidate_metadata: dict[str, Any] = {
                "bucket": candidate.bucket,
                "signals": {
                    "status": "coverage_replay",
                    "bucket": candidate.bucket,
                },
            }
            if coverage_key:
                candidate_metadata["signals"]["coverage_key"] = coverage_key
            if scheduler_uses_feedback and config["ucb_trace"]:
                candidate_metadata["_ucb_trace"] = True
            scheduler.add(candidate, metadata=candidate_metadata)
            ready_texts.add(text)
            next_discovered_ordinal_holder[0] += 1
            added_count += 1

        if added_count > 0:
            last_low_value_signature[0] = None
            log.info(
                "Replenished scheduler with %s unique-coverage stored seeds.",
                added_count,
            )
            return True
        return False

    def _try_refill_scheduler_from_grammar_coverage() -> bool:
        refill_limit = max(1, int(config.get("seed_preload_total") or 8))
        ready_texts = [item.seed.text for item in scheduler.ready_items()]
        conn_thread = open_results_db(db_path)
        try:
            history_texts = collect_history_texts(
                conn=conn_thread,
                target=effective_target,
                ready_texts=ready_texts,
            )
            refill = generate_grammar_refill_seeds(
                history_texts=history_texts,
                ready_texts=ready_texts,
                mutator_kind=effective_mutator,
                rng=rng,
                count=refill_limit,
            )
            if not refill.seeds:
                return False

            added_count = 0
            for text in refill.seeds:
                if not add_seed_input_if_new(
                    conn_thread,
                    target=effective_target,
                    input_text=text,
                ):
                    continue
                candidate = make_generated_seed(
                    text=text,
                    family=family,
                    ordinal=next_discovered_ordinal_holder[0],
                    source_prefix="grammar",
                    source_tag="grammar_generated",
                )
                candidate_metadata: dict[str, Any] = {
                    "bucket": candidate.bucket,
                    "signals": {
                        "coverage_key": {
                            "family": candidate.family,
                            "bucket": candidate.bucket,
                        },
                        "status": "grammar_generated",
                    },
                }
                if scheduler_uses_feedback and config["ucb_trace"]:
                    candidate_metadata["_ucb_trace"] = True
                scheduler.add(candidate, metadata=candidate_metadata)
                next_discovered_ordinal_holder[0] += 1
                added_count += 1

            if added_count > 0:
                last_low_value_signature[0] = None
                log.info(
                    "Grammar refill added %s candidate seeds (%s uncovered grammar items before refill).",
                    added_count,
                    len(refill.uncovered_coverage_items),
                )
                return True
            return False
        finally:
            conn_thread.close()

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
                        if startup_warm_queue or not scheduler.empty():
                            low_value_signature = None
                            if not startup_warm_queue and not scheduler.empty():
                                low_value_signature = _coverage_replay_ready_signature(
                                    scheduler.ready_items()
                                )
                            if (
                                low_value_signature is not None
                                and low_value_signature != last_low_value_signature[0]
                            ):
                                last_low_value_signature[0] = low_value_signature
                                if seed_refill_mode == "grammar":
                                    if _try_refill_scheduler_from_grammar_coverage():
                                        cond.notify_all()
                                else:
                                    # Prefer replaying previously successful
                                    # unique-coverage seeds when the pool looks stale.
                                    if _try_refill_scheduler_from_unique_coverage_store():
                                        cond.notify_all()
                            if current_mutations_left[0] <= 0:
                                conn_thread = open_results_db(db_path)
                                try:
                                    leased_from_startup_warm = False
                                    if startup_warm_queue:
                                        current_scheduled[0], leased_from_startup_warm = (
                                            _lease_next_schedulable_item(
                                                startup_warm_queue=startup_warm_queue,
                                                scheduler=scheduler,
                                            )
                                        )
                                        n = 1
                                    else:
                                        live_scheduler_seeds = [
                                            item.seed for item in scheduler.ready_items()
                                        ]
                                        stats = seed_stats_for_power_schedule(
                                            corpus=corpus,
                                            target=effective_target,
                                            conn=conn_thread,
                                            scheduler_seeds=live_scheduler_seeds,
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
                                        current_scheduled[0], leased_from_startup_warm = (
                                            _lease_next_schedulable_item(
                                                startup_warm_queue=startup_warm_queue,
                                                scheduler=scheduler,
                                            )
                                        )
                                        energy = seed_energies_holder[0].get(
                                            current_scheduled[0].seed.ordinal, 1
                                        )

                                        n = (
                                            min(max(1, energy), remaining_budget[0])
                                            if remaining_budget is not None
                                            else max(1, energy)
                                        )
                                        if scheduler_uses_feedback:
                                            n = min(n, feedback_scheduler_batch_cap)
                                    current_batch.clear()
                                    current_batch.extend(
                                        _build_execution_batch(
                                            item=current_scheduled[0],
                                            n=n,
                                            target=effective_target,
                                            conn_thread=conn_thread,
                                            generate_mutation_batch=_generate_timed_mutation_batch,
                                        )
                                    )
                                    current_mutations_left[0] = len(current_batch)
                                    if not leased_from_startup_warm:
                                        scheduler.begin_batch(
                                            current_scheduled[0],
                                            batch_size=current_mutations_left[0],
                                        )
                                    batch_expected[current_scheduled[0].item_id] = len(
                                        current_batch
                                    )
                                    mode = (
                                        "startup-warm"
                                        if leased_from_startup_warm
                                        else "single-mutation bandit"
                                        if scheduler_uses_feedback
                                        else "batch"
                                    )
                                    if debug_mode:
                                        log.info(
                                            "Scheduled seed %s with energy %s (%s unique mutations, mode=%s)",
                                            current_scheduled[0].seed.seed_id,
                                            n,
                                            len(current_batch),
                                            mode,
                                        )
                                    _sync_dashboard_worker_count()
                                    dashboard.record_schedule(
                                        pending_jobs=len(pending),
                                        scheduler_size=_ready_scheduler_size(),
                                        queue_size=len(startup_warm_queue)
                                        + len(batch_expected)
                                        + len(pending),
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
                            mutated_text, generation_time_seconds = current_batch.popleft()
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
                                "generation_time_seconds": generation_time_seconds,
                            }
                            pending[job_id] = (scheduled, iteration)
                            reply_queues[wid].put(work)
                            break
                        while (
                            not startup_warm_queue
                            and scheduler.empty()
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
                        if startup_warm_queue or not scheduler.empty():
                            continue
                        if seed_refill_mode == "grammar":
                            if _try_refill_scheduler_from_grammar_coverage():
                                cond.notify_all()
                                continue
                        else:
                            if _try_refill_scheduler_from_unique_coverage_store():
                                cond.notify_all()
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

    def _sync_dashboard_worker_count() -> None:
        if use_live_ui:
            dashboard.update_worker_count(
                active_workers=sum(1 for proc in procs if proc.is_alive())
            )
            dashboard.update_busy_workers(busy_workers=len(pending))

    def _spawn_worker(slot: int) -> Process:
        proc = Process(
            target=run_worker_process,
            args=(
                config,
                request_queue,
                reply_queues,
                result_queue,
                slot,
                str(results_folder),
                effective_mutator,
            ),
        )
        proc.start()
        return proc

    procs = [_spawn_worker(w) for w in range(workers)]
    pending_worker_refresh: dict[int, str] = {}
    _sync_dashboard_worker_count()

    def _maybe_refresh_workers() -> None:
        if shutdown_requested[0]:
            _sync_dashboard_worker_count()
            return
        for slot, proc in enumerate(procs):
            if proc.is_alive():
                continue
            proc.join(timeout=0.1)
            reason = pending_worker_refresh.pop(slot, None)
            if reason is None:
                reason = "unexpected exit"
                log.warning(
                    "Worker slot w%s pid=%s exited unexpectedly; spawning replacement.",
                    slot,
                    proc.pid,
                )
            procs[slot] = _spawn_worker(slot)
            log.info(
                "Spawned replacement worker w%s pid=%s after %s.",
                slot,
                procs[slot].pid,
                reason,
            )
        _sync_dashboard_worker_count()

    memory_telemetry_seconds = float(config.get("memory_telemetry_seconds") or 0.0)
    memory_telemetry_stop = threading.Event()
    memory_telemetry_thread: threading.Thread | None = None
    if memory_telemetry_seconds > 0:
        main_pid = os.getpid()

        def _memory_telemetry_loop() -> None:
            while not memory_telemetry_stop.wait(memory_telemetry_seconds):
                total_kib = 0
                main_rss_kib = _read_rss_kib(main_pid)
                if main_rss_kib is not None:
                    total_kib += main_rss_kib
                parts = [f"main[pid={main_pid}]={_format_rss(main_rss_kib)}"]
                for idx, proc in enumerate(procs):
                    pid = proc.pid
                    if pid is None:
                        parts.append(f"w{idx}[pid=?]=starting")
                        continue
                    rss_kib = _read_rss_kib(pid)
                    if rss_kib is not None:
                        total_kib += rss_kib
                    state = "alive" if proc.is_alive() else "dead"
                    parts.append(
                        f"w{idx}[{state},pid={pid}]={_format_rss(rss_kib)}"
                    )
                total_rss = _format_rss(total_kib)
                detail = " | ".join(parts)
                _sync_dashboard_worker_count()
                dashboard.update_memory_telemetry(
                    total_rss=total_rss,
                    details=detail,
                )
                if not use_live_ui:
                    log.info(
                        "RSS telemetry: total=%s | %s",
                        total_rss,
                        detail,
                    )

        memory_telemetry_thread = threading.Thread(
            target=_memory_telemetry_loop,
            name="memory-telemetry",
            daemon=True,
        )
        memory_telemetry_thread.start()
        if not use_live_ui:
            log.info(
                "Memory telemetry enabled: sampling coordinator + worker RSS every %.1fs",
                memory_telemetry_seconds,
            )

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
                    _maybe_refresh_workers()
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
                            dashboard.update_status(
                                "STOPPING",
                                event="forcing shutdown after idle timeout",
                            )
                            if use_live_ui:
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
                        dashboard.update_status(
                            "STOPPING", event="timed out waiting for workers"
                        )
                        if use_live_ui:
                            live.update(dashboard.render())
                        break
                    if shutdown_requested[0]:
                        _maybe_refresh_workers()
                        dashboard.update_status(
                            "STOPPING", event="shutdown requested"
                        )
                        if use_live_ui:
                            live.update(dashboard.render())
                        break
                    if use_live_ui:
                        _maybe_refresh_workers()
                        live.update(dashboard.render())
                    continue
                except InterruptedError:
                    # System call interrupted by signal; if a shutdown has been
                    # requested, stop waiting for more results.
                    if shutdown_requested[0]:
                        if use_live_ui:
                            _maybe_refresh_workers()
                            dashboard.update_status(
                                "STOPPING", event="shutdown requested"
                            )
                            live.update(dashboard.render())
                        break
                    if use_live_ui:
                        _maybe_refresh_workers()
                        live.update(dashboard.render())
                    continue

                signals_raw = result.get("signals")
                signals = dict(signals_raw) if isinstance(signals_raw, dict) else {}

                with cond:
                    worker_id = result.get("worker_id")
                    worker_retiring = bool(result.get("worker_retiring"))
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
                    new_bug = _claim_new_bug_flag(
                        seen_bug_keys=seen_bug_keys,
                        result=result,
                    )
                    signals["new_bug"] = new_bug
                    result["signals"] = signals
                    if scheduled is not None:
                        item_id = scheduled.item_id
                        score = result["isinteresting_score"]
                        if bool(scheduled.metadata.get("startup_warm_pending_insert")):
                            batch_expected.pop(item_id, None)
                            scheduled = _reinsert_startup_warm_seed(
                                scheduler=scheduler,
                                item=scheduled,
                                isinteresting_score=score,
                                signals=result.get("signals"),
                            )
                            cond.notify_all()
                        elif scheduler_uses_feedback:
                            scheduled = scheduler.update(
                                scheduled,
                                isinteresting_score=score,
                                signals=result["signals"],
                            )
                            _clear_completed_feedback_batch(
                                batch_expected=batch_expected,
                                item=scheduled,
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
                covered_edges = result.pop("covered_edges", ())
                total_branches = max(0, int(result.pop("total_branches", 0) or 0))
                coverage_backend = str(result.pop("coverage_backend", "") or "").strip()
                coverage_source_kind = str(
                    result.pop("coverage_source_kind", "") or ""
                ).strip()
                covered_lines = max(0, int(result.pop("covered_lines", 0) or 0))
                total_lines = max(0, int(result.pop("total_lines", 0) or 0))
                total_edges = max(0, int(result.pop("total_edges", 0) or 0))
                result.pop("worker_id", None)
                result.pop("worker_retiring", None)
                coverage_key = _coverage_key_from_signals(signals)
                new_coverage = bool(signals.get("new_coverage"))
                insert_run(
                    conn,
                    iteration=iteration,
                    seed_id=result["seed_id"],
                    seed_bucket=str(signals.get("bucket") or ""),
                    seed_text=result["seed_text"],
                    mutated_input=result["mutated_input"],
                    generation_time_seconds=result.get("generation_time_seconds", 0.0),
                    run_time_seconds=result.get("run_time_seconds", 0.0),
                    status=result["status"],
                    bug_signature=result["bug_signature"],
                    coverage_key=coverage_key,
                    new_coverage=new_coverage,
                    new_bug=new_bug,
                    new_diff_behavior=bool(signals.get("new_differential_behavior")),
                    new_error_site=bool(signals.get("new_error_site")),
                    new_edge_count=signals.get("new_edge_count"),
                    rare_edge_score=signals.get("rare_edge_score"),
                    isinteresting_score=result["isinteresting_score"],
                    parse_category=str(signals.get("parse_category") or ""),
                    output_signature=str(signals.get("output_signature") or ""),
                    error_code=str(signals.get("error_code") or ""),
                    diff_behavior_key=str(signals.get("diff_behavior_key") or ""),
                    diff_pattern_key=str(signals.get("diff_pattern_key") or ""),
                    mismatch_type_key=str(signals.get("mismatch_type_key") or ""),
                    structure_key=str(signals.get("structure_key") or ""),
                    length_bucket=str(signals.get("length_bucket") or ""),
                    representative_key=str(signals.get("representative_key") or ""),
                    late_parse_depth=signals.get("late_parse_depth"),
                    execution_stability_bonus=signals.get("execution_stability_bonus"),
                    target=effective_target,
                )
                newest_coverage_branch = ""
                if new_coverage and covered_edges:
                    newest_coverage_branch = _find_newest_unseen_branch(conn, covered_edges)
                if covered_edges:
                    insert_seen_edges_into_conn(conn, covered_edges)
                    increment_edge_observations(
                        conn,
                        target=effective_target,
                        edges=covered_edges,
                    )
                covered_branches = _count_seen_covered_branches(conn)
                unique_covered_arcs = _count_seen_branches(conn)
                if new_coverage and coverage_key:
                    add_unique_coverage_seed_input(
                        conn,
                        target=effective_target,
                        coverage_key=coverage_key,
                        input_text=result["mutated_input"],
                        seed_family=family,
                        seed_bucket=str(signals.get("bucket") or "discovered"),
                    )
                with cond:
                    parent_signals = (result.get("signals") or {}).copy()
                    parent_signals["new_coverage"] = new_coverage
                    parent_signals["new_bug"] = new_bug
                    candidate = make_discovered_seed(
                        result["mutated_input"],
                        family,
                        parent_signals.get("bucket", "discovered"),
                        next_discovered_ordinal_holder[0],
                    )
                    candidate_metadata: dict[str, Any] = {
                        "bucket": candidate.bucket,
                        "parent_seed_id": result["seed_id"],
                        "initial_isinteresting_score": result["isinteresting_score"],
                        "signals": parent_signals,
                    }
                    if scheduler_uses_feedback and config["ucb_trace"]:
                        candidate_metadata["_ucb_trace"] = True
                    accepted = scheduler.consider_seed(candidate, metadata=candidate_metadata)
                    if accepted is not None:
                        add_seed_input_if_new(
                            conn,
                            target=effective_target,
                            input_text=result["mutated_input"],
                        )
                        next_discovered_ordinal_holder[0] += 1
                        last_low_value_signature[0] = None
                        cond.notify()
                results_received += 1
                last_result_time = time.time()
                with cond:
                    last_low_value_signature[0] = None
                    results_received_count[0] = results_received
                    pending_jobs = len(pending)
                    scheduler_size = _ready_scheduler_size()
                    queue_size = len(startup_warm_queue) + len(batch_expected) + len(pending)
                    cond.notify()
                if use_live_ui:
                    _sync_dashboard_worker_count()

                status = str(result.get("status") or "").strip().lower() or "unknown"
                if mutator_feedback_fn is not None:
                    gained_novelty = any(
                        bool(signals.get(key))
                        for key in (
                            "new_coverage",
                            "new_bug",
                            "new_bug_site",
                            "new_exception_site",
                            "new_differential_behavior",
                        )
                    )
                    mutator_feedback_fn(
                        mutated_text=result["mutated_input"],
                        gained_coverage=gained_novelty,
                    )

                event_bits = [f"iter {iteration}", status]
                if new_bug:
                    event_bits.append("new bug")
                elif bool(signals.get("new_bug_site")):
                    event_bits.append("new bug site")
                elif bool(signals.get("new_exception_site")):
                    event_bits.append("new exception site")
                if bool(signals.get("new_differential_behavior")):
                    event_bits.append("differential")
                if new_coverage:
                    event_bits.append("new coverage")
                _maybe_refresh_workers()
                dashboard.record_result(
                    iteration=int(iteration),
                    status=status,
                    score=result["isinteresting_score"],
                    new_coverage=new_coverage,
                    new_bug=new_bug,
                    covered_branches=covered_branches,
                    total_branches=total_branches,
                    coverage_backend=coverage_backend,
                    coverage_source_kind=coverage_source_kind,
                    covered_lines=covered_lines,
                    total_lines=total_lines,
                    total_edges=total_edges,
                    unique_covered_arcs=unique_covered_arcs,
                    pending_jobs=pending_jobs,
                    scheduler_size=scheduler_size,
                    queue_size=queue_size,
                    event=" | ".join(event_bits),
                    mutated_input=result["mutated_input"],
                    newest_coverage_branch=newest_coverage_branch,
                    bug_signature=result.get("bug_signature"),
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
                if (
                    worker_retiring
                    and isinstance(worker_id, int)
                    and 0 <= worker_id < len(procs)
                    and not shutdown_requested[0]
                ):
                    pending_worker_refresh[worker_id] = (
                        f"recycling after {worker_max_jobs} jobs"
                    )
                    log.info(
                        "Worker slot w%s marked for recycle after %s jobs; waiting for clean exit.",
                        worker_id,
                        worker_max_jobs,
                    )
                _maybe_refresh_workers()
                if use_live_ui:
                    live.update(dashboard.render())
                elif pbar is not None:
                    pbar.update(1)
                if total_jobs[0] > 0 and results_received >= total_jobs[0]:
                    _maybe_refresh_workers()
                    dashboard.update_status("DONE", event="run complete")
                    if use_live_ui:
                        live.update(dashboard.render())
                    break
                if shutdown_requested[0]:
                    # On explicit shutdown (Ctrl+C), stop waiting for any
                    # remaining in-flight work; we'll tear down workers below.
                    _maybe_refresh_workers()
                    dashboard.update_status("STOPPING", event="shutdown requested")
                    if use_live_ui:
                        live.update(dashboard.render())
                    break
            if pbar is not None:
                pbar.close()
    finally:
        memory_telemetry_stop.set()
        if memory_telemetry_thread is not None:
            memory_telemetry_thread.join(timeout=1.0)
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
        _sync_dashboard_worker_count()
        dashboard.save_artifacts(results_folder)
