from __future__ import annotations

import argparse
import csv
import hashlib
import os
import random
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from multiprocessing import Process, Queue, current_process
from pathlib import Path
from typing import Any, Callable, TypedDict

from isinteresting import (
    get_covered_edges_from_result,
    get_compute_interestingness,
    list_versions as isinteresting_versions,
)
from mutator import get_mutator, list_versions as mutator_versions
from parser import DEFAULT_TIMEOUT, TARGETS, get_parser, list_versions as parser_versions
from power_scheduler import SeedStats, get_power_scheduler, list_versions as power_scheduler_versions
from seed_corpus import Seed, get_corpus_loader, list_versions as seed_corpus_versions
from seed_scheduler import BaseSeedScheduler, get_scheduler, list_versions as scheduler_versions, ScheduledSeed

FUZZER_ROOT = Path(__file__).resolve().parent
RESULTS_DIR = FUZZER_ROOT / "results"
DISCOVERED_SEED_ORDINAL_BASE = 1_000_000
JSON_DECODER_TARGET_DIR = FUZZER_ROOT / "targets" / "json-decoder"
JSON_DECODER_STV_SCRIPT = JSON_DECODER_TARGET_DIR / "json_decoder_stv.py"


class FuzzConfig(TypedDict):
    target: str
    scheduler_kind: str
    mutator_kind: str
    max_iterations: int | None
    max_hours: float | None
    timeout: float
    rng_seed: int | None
    workers: int
    isinteresting_version: str
    mutator_version: str
    parser_version: str
    power_scheduler_version: str
    seed_corpus_version: str


def build_config() -> FuzzConfig:
    parser = argparse.ArgumentParser(
        description="AFL-style fuzzer harness wiring seed corpus, mutator, parser, "
        "interestingness scoring, schedulers, and power scheduling. "
    )
    parser.add_argument(
        "--target",
        default="json-decoder",
        choices=sorted(TARGETS.keys()),
        help="Target name (must be a key in parser.TARGETS).",
    )
    parser.add_argument(
        "--scheduler",
        dest="scheduler_kind",
        default="heap",
        choices=list(scheduler_versions()),
        help="Seed scheduler version.",
    )
    parser.add_argument(
        "--mutator",
        dest="mutator_kind",
        default="auto",
        choices=["auto", "json", "ip"],
        help="Mutation mode: auto-detect from target, or force json/ip.",
    )
    parser.add_argument(
        "--iterations",
        dest="max_iterations",
        type=int,
        default=10,
        help="Maximum number of fuzzing iterations (mutually exclusive with --hours).",
    )
    parser.add_argument(
        "--hours",
        dest="max_hours",
        type=float,
        default=None,
        help="Maximum fuzzing time in hours (mutually exclusive with --iterations).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="Per-run timeout in seconds.",
    )
    parser.add_argument(
        "--seed",
        dest="rng_seed",
        type=int,
        default=None,
        help="Optional RNG seed for reproducibility.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of worker processes. All workers share one scheduler.",
    )
    parser.add_argument(
        "--isinteresting-version",
        dest="isinteresting_version",
        default="base",
        choices=list(isinteresting_versions()),
        help="Interestingness module version for ablation.",
    )
    parser.add_argument(
        "--mutator-version",
        dest="mutator_version",
        default="base",
        choices=list(mutator_versions()),
        help="Mutator module version for ablation.",
    )
    parser.add_argument(
        "--parser-version",
        dest="parser_version",
        default="base",
        choices=list(parser_versions()),
        help="Parser module version for ablation.",
    )
    parser.add_argument(
        "--power-scheduler-version",
        dest="power_scheduler_version",
        default="base",
        choices=list(power_scheduler_versions()),
        help="Power scheduler module version for ablation.",
    )
    parser.add_argument(
        "--seed-corpus-version",
        dest="seed_corpus_version",
        default="base",
        choices=list(seed_corpus_versions()),
        help="Seed corpus module version for ablation.",
    )

    args = parser.parse_args()

    if args.max_hours is not None and "--iterations" in sys.argv:
        parser.error("Cannot specify both --iterations and --hours; use exactly one.")
    if args.max_hours is not None:
        if args.max_hours <= 0:
            parser.error("--hours must be positive.")

    max_iterations: int | None = None if args.max_hours is not None else args.max_iterations
    return {
        "target": args.target,
        "scheduler_kind": args.scheduler_kind,
        "mutator_kind": args.mutator_kind,
        "max_iterations": max_iterations,
        "max_hours": args.max_hours,
        "timeout": args.timeout,
        "rng_seed": args.rng_seed,
        "workers": args.workers,
        "isinteresting_version": args.isinteresting_version,
        "mutator_version": args.mutator_version,
        "parser_version": args.parser_version,
        "power_scheduler_version": args.power_scheduler_version,
        "seed_corpus_version": args.seed_corpus_version,
    }


def infer_mutator_kind(*, mutator_kind: str, target: str) -> str:
    if mutator_kind != "auto":
        return mutator_kind

    target_lower = target.lower()
    if "json" in target_lower:
        return "json"
    if "ipv4" in target_lower or "ipv6" in target_lower or "cidr" in target_lower:
        return "ip"
    return "json"


def get_seed_stats_from_db(
    conn: sqlite3.Connection,
    corpus: Any,
    target: str,
) -> list[SeedStats]:
    """
    Aggregate runs by seed_id from the DB and return SeedStats keyed by ordinal.
    Includes fuzz_count (times this seed was used), avg_isinteresting_score, bug_count.
    """
    target_set = corpus.target(target)
    seed_id_to_ordinal = {s.seed_id: s.ordinal for s in target_set.seeds}
    cur = conn.execute(
        """
        SELECT seed_id,
               COUNT(*) AS fuzz_count,
               AVG(isinteresting_score) AS avg_isinteresting_score,
               SUM(CASE WHEN status IN ('bug', 'crash', 'timeout') THEN 1 ELSE 0 END) AS bug_count
        FROM runs
        WHERE target = ?
        GROUP BY seed_id
        """,
        (target,),
    )
    by_seed_id: dict[str, dict[str, Any]] = {}
    for row in cur:
        seed_id, fuzz_count, avg_score, bug_count = row
        by_seed_id[str(seed_id)] = {
            "fuzz_count": fuzz_count or 0,
            "avg_isinteresting_score": float(avg_score) if avg_score is not None else None,
            "bug_count": bug_count or 0,
        }
    stats: list[SeedStats] = []
    for seed in target_set.seeds:
        row = by_seed_id.get(seed.seed_id, {})
        stat: SeedStats = {
            "id": seed.ordinal,
            "fuzz_count": row.get("fuzz_count", 0),
        }
        if row.get("avg_isinteresting_score") is not None:
            stat["avg_isinteresting_score"] = row["avg_isinteresting_score"]
        if row.get("bug_count", 0) > 0:
            stat["bug_count"] = row["bug_count"]
        stats.append(stat)
    return stats


def seed_stats_for_power_schedule(
    *,
    corpus: Any,
    target: str,
    conn: sqlite3.Connection | None = None,
) -> list[SeedStats]:
    """Build SeedStats for the power scheduler; use DB aggregates when conn is provided."""
    target_set = corpus.target(target)
    if conn is None:
        return [{"id": seed.ordinal, "fuzz_count": 0} for seed in target_set.seeds]
    return get_seed_stats_from_db(conn=conn, corpus=corpus, target=target)


def warmup_power_schedule(
    *,
    corpus: Any,
    target: str,
    power_scheduler_module: Any,
    conn: sqlite3.Connection | None = None,
) -> dict[int, int]:
    stats = seed_stats_for_power_schedule(corpus=corpus, target=target, conn=conn)
    if not stats:
        return {}
    schedule = power_scheduler_module.compute_power_schedule(seeds=stats)
    return dict(schedule["seed_energies"])


def init_scheduler(
    *,
    corpus: Any,
    target: str,
    scheduler_kind: str,
    get_scheduler_fn: Any,
) -> BaseSeedScheduler:
    scheduler = get_scheduler_fn(scheduler_kind)
    target_set = corpus.target(target)
    for seed in target_set.seeds:
        scheduler.add(seed, metadata={"bucket": seed.bucket})
    return scheduler


def _make_discovered_seed(
    mutated_text: str,
    family: str,
    parent_bucket: str,
    ordinal: int,
) -> Seed:
    """Build a Seed for a mutated input that was interesting (e.g. new coverage)."""
    text_bytes = mutated_text.encode("utf-8")
    fp = hashlib.sha256(text_bytes).hexdigest()[:16]
    seed_id = f"discovered-{fp}"
    return Seed(
        seed_id=seed_id,
        family=family,
        bucket="discovered",
        label=seed_id,
        text=mutated_text,
        tags=(),
        expected="",
        ordinal=ordinal,
        fingerprint=fp,
    )


def _init_results_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            iteration INTEGER NOT NULL,
            seed_id TEXT NOT NULL,
            seed_text TEXT,
            mutated_input TEXT NOT NULL,
            status TEXT,
            bug_type TEXT,
            exception TEXT,
            message TEXT,
            file TEXT,
            line INTEGER,
            isinteresting_score REAL,
            target TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_branches (
            file TEXT NOT NULL,
            from_line INTEGER NOT NULL,
            to_line INTEGER NOT NULL,
            PRIMARY KEY (file, from_line, to_line)
        )
    """)
    conn.commit()


def _insert_run(
    conn: sqlite3.Connection,
    *,
    iteration: int,
    seed_id: str,
    seed_text: str,
    mutated_input: str,
    status: str | None,
    bug_signature: dict[str, Any] | None,
    isinteresting_score: float,
    target: str,
) -> None:
    bug_type = (bug_signature or {}).get("type")
    exc = (bug_signature or {}).get("exception")
    msg = (bug_signature or {}).get("message")
    file_ = (bug_signature or {}).get("file")
    line_raw = (bug_signature or {}).get("line")
    line = int(line_raw) if line_raw is not None and str(line_raw).isdigit() else None
    conn.execute(
        """INSERT INTO runs (
            iteration, seed_id, seed_text, mutated_input, status, bug_type,
            exception, message, file, line, isinteresting_score, target, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            iteration,
            seed_id,
            seed_text or "",
            mutated_input,
            status,
            bug_type,
            exc,
            msg,
            file_,
            line,
            isinteresting_score,
            target,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def _input_already_run(
    conn: sqlite3.Connection,
    mutated_input: str,
    target: str,
) -> bool:
    """Return True if this (mutated_input, target) has already been run and recorded in runs."""
    cur = conn.execute(
        "SELECT 1 FROM runs WHERE mutated_input = ? AND target = ? LIMIT 1",
        (mutated_input, target),
    )
    return cur.fetchone() is not None


def _generate_unique_mutations(
    n: int,
    seed_text: str,
    mutate_fn: Callable[..., str],
    mutator_kind: str,
    rng: random.Random,
    conn: sqlite3.Connection,
    target: str,
    *,
    max_attempts: int = 200,
) -> list[str]:
    """Generate up to n unique mutated inputs not already present in runs for this target."""
    seen: set[str] = set()
    batch: list[str] = []
    for _ in range(n):
        candidate = mutate_fn(
            seed_text,
            mutator_kind=mutator_kind,
            rng=rng,
        )
        for attempt in range(max_attempts):
            if candidate not in seen and not _input_already_run(conn, candidate, target):
                seen.add(candidate)
                batch.append(candidate)
                break
            candidate = mutate_fn(
                seed_text,
                mutator_kind=mutator_kind,
                rng=rng,
            )
        else:
            seen.add(candidate)
            batch.append(candidate)
    return batch


def _insert_seen_branches(db_path: Path | str, result: dict[str, Any]) -> None:
    """Insert covered branches from the parser result into seen_branches. Called by main after each run."""
    edges = get_covered_edges_from_result(result)
    if not edges:
        return
    path = Path(db_path) if isinstance(db_path, str) else db_path
    if not path.exists():
        return
    try:
        conn = sqlite3.connect(str(path))
        try:
            for (f, fl, tl) in edges:
                conn.execute(
                    "INSERT OR IGNORE INTO seen_branches (file, from_line, to_line) VALUES (?, ?, ?)",
                    (f, fl, tl),
                )
            conn.commit()
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        pass


def get_inputs_for_unique_error_line_pairs(
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """
    Return rows for each unique (exception, line) pair that had a bug/crash/timeout,
    with one representative input per pair (seed_id, mutated_input, etc.).
    """
    cur = conn.execute("""
        SELECT exception, line, file, bug_type,
               seed_id, seed_text, mutated_input, status, iteration, isinteresting_score
        FROM runs
        WHERE status IN ('bug', 'crash', 'timeout') AND (exception IS NOT NULL OR line IS NOT NULL)
        ORDER BY exception, line
    """)
    rows = cur.fetchall()
    # Group by (exception, line) and keep first occurrence as representative
    seen: set[tuple[str | None, int | None]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        exc, line, file_, bug_type, seed_id, seed_text, mutated_input, status, iteration, score = row
        key = (exc, line)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "exception": exc,
            "line": line,
            "file": file_,
            "bug_type": bug_type,
            "seed_id": seed_id,
            "seed_text": seed_text,
            "mutated_input": mutated_input,
            "status": status,
            "iteration": iteration,
            "isinteresting_score": score,
        })
    return out


def _export_results(
    *,
    results_folder: Path,
    db_path: Path,
    target: str,
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        # 1. Unique (error, line) pairs CSV
        pairs = get_inputs_for_unique_error_line_pairs(conn)
        pairs_path = results_folder / "unique_error_line_pairs.csv"
        if pairs:
            with open(pairs_path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(
                    f,
                    fieldnames=[
                        "exception", "line", "file", "bug_type", "seed_id", "seed_text",
                        "mutated_input", "status", "iteration", "isinteresting_score",
                    ],
                )
                w.writeheader()
                w.writerows(pairs)
        else:
            with open(pairs_path, "w", newline="", encoding="utf-8") as f:
                f.write("exception,line,file,bug_type,seed_id,seed_text,mutated_input,status,iteration,isinteresting_score\n")

        # 2. Full runs as CSV
        runs_path = results_folder / "runs.csv"
        cur = conn.execute(
            "SELECT iteration, seed_id, seed_text, mutated_input, status, bug_type, "
            "exception, message, file, line, isinteresting_score, target, created_at FROM runs"
        )
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        with open(runs_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(rows)
    finally:
        conn.close()

    if target == "json-decoder" and JSON_DECODER_STV_SCRIPT.is_file():
        print("Running json_decoder_stv.py with for each input that triggered a unique (error, line)")
        # Rerun json_decoder_stv.py with --show-coverage for each input that triggered a unique (error, line)
        stv_logs_dir = JSON_DECODER_TARGET_DIR / "logs"
        stv_logs_dir.mkdir(parents=True, exist_ok=True)
        stv_csv = stv_logs_dir / "bug_counts.csv"
        # Start fresh so the copied CSV only contains this export run's STV results
        if stv_csv.is_file():
            stv_csv.unlink()
        coverage_file = str((results_folder / ".coverage_buggy_json").resolve())
        print(f"Coverage file: {coverage_file}")
        for rec in pairs:
            print(f"Running json_decoder_stv.py with input: {rec.get('mutated_input')}")
            input_text = rec.get("mutated_input") or ""
            if not input_text:
                continue
            # Use sys.executable + PYTHONPATH so the JSON string is passed as one argv (uv re-parses and breaks on quotes)
            env = os.environ.copy()
            env["PYTHONPATH"] = os.pathsep.join(
                [str(JSON_DECODER_TARGET_DIR), env.get("PYTHONPATH", "")]
            )
            proc = subprocess.run(
                [
                    sys.executable,
                    str(JSON_DECODER_STV_SCRIPT),
                    "--str-json",
                    input_text,
                    "--show-coverage",
                    "--coverage-file",
                    coverage_file,
                ],
                cwd=str(JSON_DECODER_TARGET_DIR),
                env=env,
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0 and proc.stderr:
                print(f"STV script stderr: {proc.stderr}", file=sys.stderr)
        if stv_csv.is_file():
            dest = results_folder / "bug_counts.csv"
            shutil.copy2(stv_csv, dest)


def _run_worker_process(
    config: FuzzConfig,
    request_queue: Queue,
    reply_queue: Queue,
    result_queue: Queue,
    worker_id: int,
    results_folder_str: str,
    effective_mutator: str,
) -> None:
    """Run fuzz loop in a worker: request work from coordinator, run one iteration, send result.
    Mutated input is pre-generated by the coordinator so workers only run the parser.
    """
    parser_api = get_parser(config["parser_version"])
    compute_interestingness_fn = get_compute_interestingness(config["isinteresting_version"])
    effective_target = config["target"]
    results_folder = Path(results_folder_str)

    while True:
        request_queue.put(1)
        work = reply_queue.get()
        if work is None:
            break

        job_id = work["job_id"]
        item_id = work["item_id"]
        iteration = work["iteration"]
        seed_id = work["seed_id"]
        seed_text = work["seed_text"]
        bucket = work["bucket"]
        mutated_text = work["mutated_text"]
        result = parser_api.run_parser(
            input_data=mutated_text.encode("utf-8"),
            target=effective_target,
            timeout=config["timeout"],
            print_json=False,
        )
        db_path = results_folder / "runs.db"
        score = compute_interestingness_fn(
            result=result,
            db_path=db_path,
            target=work.get("target", ""),
        )
        _insert_seen_branches(db_path, result)
        closed = result.get("closed_result", {})
        signals: dict[str, Any] = {
            "iteration": iteration,
            "seed_id": seed_id,
            "bucket": bucket,
            "status": closed.get("status"),
            "isinteresting": score,
        }
        result_queue.put({
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
        })


def _run_fuzzer_multi_worker(
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
    """Run fuzzer with one shared scheduler in the main process and N workers.
    Power schedule is recomputed from the DB every time we assign energy to a new seed.
    Mutations are pre-generated in the coordinator so each work item has a unique mutated_input.
    """
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

    def request_handler() -> None:
        nones_sent = 0
        while nones_sent < workers:
            request_queue.get()
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
                                    schedule = power_scheduler_module.compute_power_schedule(
                                        seeds=stats
                                    )
                                    seed_energies_holder[0] = dict(
                                        schedule["seed_energies"]
                                    )
                                current_scheduled[0] = scheduler.next()
                                energy = seed_energies_holder[0].get(
                                    current_scheduled[0].seed.ordinal, 1
                                )
                                n = (
                                    min(max(1, energy), remaining_budget[0])
                                    if remaining_budget is not None
                                    else max(1, energy)
                                )
                                current_batch.clear()
                                current_batch.extend(
                                    _generate_unique_mutations(
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
                                print(
                                    f"Scheduled seed {current_scheduled[0].seed.seed_id} with energy {energy} ({len(current_batch)} unique mutations)"
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
                            "bucket": scheduled.seed.bucket,
                            "target": effective_target,
                            "mutated_text": mutated_text,
                        }
                        pending[job_id] = (scheduled, iteration)
                        reply_queue.put(work)
                        break
                    # Scheduler empty: wait for new work (discovered seed) or until all results in
                    while scheduler.empty() and results_received_count[0] < iteration_counter[0]:
                        cond.wait()
                    if not scheduler.empty():
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
            target=_run_worker_process,
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
    batch_last_signals_by_item: dict[str, dict[str, Any]] = {}
    while True:
        result = result_queue.get()
        with cond:
            job_id = result["job_id"]
            scheduled, iteration = pending.pop(job_id)
            item_id = scheduled.item_id
            score = result["isinteresting_score"]
            batch_scores_by_item.setdefault(item_id, []).append(score)
            batch_last_signals_by_item[item_id] = result["signals"]
            expected = batch_expected.get(item_id, 1)
            if len(batch_scores_by_item[item_id]) >= expected:
                scores = batch_scores_by_item.pop(item_id, [])
                signals = batch_last_signals_by_item.pop(item_id, {})
                batch_expected.pop(item_id, None)
                if scores:
                    avg_score = sum(scores) / len(scores)
                    scheduler.update(
                        scheduled,
                        isinteresting_score=avg_score,
                        signals=signals,
                    )
        _insert_run(
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
            if result["isinteresting_score"] > 0 and result["mutated_input"] not in added_seed_inputs_holder[0]:
                parent_bucket = (result.get("signals") or {}).get("bucket", "discovered")
                candidate = _make_discovered_seed(
                    result["mutated_input"],
                    family,
                    parent_bucket,
                    next_discovered_ordinal_holder[0],
                )
                scheduler.add(candidate, metadata={"bucket": candidate.bucket})
                added_seed_inputs_holder[0].add(result["mutated_input"])
                next_discovered_ordinal_holder[0] += 1
                cond.notify()
        results_received += 1
        with cond:
            results_received_count[0] = results_received
            cond.notify()
        # if iteration % 100 == 0 or result.get("status") in ("bug", "crash", "timeout"):
        if iteration:
            print(
                f"[iter {iteration}] seed={result['seed_id']} "
                f"score={result['isinteresting_score']:.3f} status={result['status']} input={result['seed_text']} mutated input={result['mutated_input']}"
            )
        if total_jobs[0] > 0 and results_received >= total_jobs[0]:
            break

    request_thread.join()
    for p in procs:
        p.join()


def run_fuzzer(config: FuzzConfig) -> None:
    corpus_loader = get_corpus_loader(config["seed_corpus_version"])
    corpus = corpus_loader.load()

    parser_api = get_parser(config["parser_version"])
    mutate_fn = get_mutator(config["mutator_version"])
    compute_interestingness_fn = get_compute_interestingness(config["isinteresting_version"])
    power_scheduler_module = get_power_scheduler(config["power_scheduler_version"])

    effective_target = config["target"]
    effective_mutator = infer_mutator_kind(
        mutator_kind=config["mutator_kind"],
        target=effective_target,
    )

    rng = random.Random(config["rng_seed"]) if config["rng_seed"] is not None else random.Random()

    scheduler = init_scheduler(
        corpus=corpus,
        target=effective_target,
        scheduler_kind=config["scheduler_kind"],
        get_scheduler_fn=get_scheduler,
    )

    if not scheduler or scheduler.empty():
        return

    timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    results_folder = RESULTS_DIR / f"{effective_target}_{timestamp_str}"
    results_folder.mkdir(parents=True, exist_ok=True)
    db_path = results_folder / "runs.db"
    conn = sqlite3.connect(str(db_path))
    _init_results_db(conn)

    seed_energies = warmup_power_schedule(
        corpus=corpus,
        target=effective_target,
        power_scheduler_module=power_scheduler_module,
        conn=conn,
    )

    shutdown_requested: list[bool] = [False]

    def _sigint_handler(_signum: int, _frame: object) -> None:
        shutdown_requested[0] = True
        print("\nCtrl+C: shutting down gracefully (workers will finish current run and exit)...", flush=True)

    try:
        signal.signal(signal.SIGINT, _sigint_handler)
    except (ValueError, OSError):
        pass

    workers = max(1, config["workers"])
    if workers > 1:
        _run_fuzzer_multi_worker(
            config=config,
            scheduler=scheduler,
            seed_energies=seed_energies,
            corpus=corpus,
            power_scheduler_module=power_scheduler_module,
            effective_target=effective_target,
            effective_mutator=effective_mutator,
            results_folder=results_folder,
            db_path=db_path,
            conn=conn,
            workers=workers,
            shutdown_requested=shutdown_requested,
            mutate_fn=mutate_fn,
            rng=rng,
        )
        conn.close()
        _export_results(
            results_folder=results_folder,
            db_path=db_path,
            target=effective_target,
        )
        return

    max_hours_single = config.get("max_hours")
    start_time_single = time.time()
    remaining: int | None = config["max_iterations"]
    iteration = 0
    target_set = corpus.target(effective_target)
    family = target_set.family
    added_seed_inputs: set[str] = set()
    next_discovered_ordinal = DISCOVERED_SEED_ORDINAL_BASE

    def _time_limit_exceeded() -> bool:
        if max_hours_single is None:
            return False
        return (time.time() - start_time_single) >= max_hours_single * 3600

    while (
        (remaining is None or remaining > 0)
        and not _time_limit_exceeded()
        and not scheduler.empty()
        and not shutdown_requested[0]
    ):
        stats = seed_stats_for_power_schedule(
            corpus=corpus, target=effective_target, conn=conn
        )
        if stats:
            schedule = power_scheduler_module.compute_power_schedule(seeds=stats)
            seed_energies = dict(schedule["seed_energies"])
        scheduled = scheduler.next()
        seed = scheduled.seed
        energy = seed_energies.get(seed.ordinal, 1)
        n = (
            min(max(1, energy), remaining)
            if remaining is not None
            else max(1, energy)
        )
        mutation_batch = _generate_unique_mutations(
            n,
            seed.text,
            mutate_fn,
            effective_mutator,
            rng,
            conn,
            effective_target,
        )
        print(
            f"Running {len(mutation_batch)} mutations for seed {seed.seed_id} with energy {energy}"
        )

        batch_scores: list[float] = []
        last_signals: dict[str, Any] = {}
        for mutated_text in mutation_batch:
            result = parser_api.run_parser(
                input_data=mutated_text.encode("utf-8"),
                target=effective_target,
                timeout=config["timeout"],
                print_json=False,
            )

            score = compute_interestingness_fn(
                result=result,
                db_path=db_path,
                target=effective_target,
            )
            batch_scores.append(score)
            last_signals = {
                "iteration": iteration,
                "seed_id": seed.seed_id,
                "bucket": seed.bucket,
                "status": result.get("closed_result", {}).get("status"),
                "isinteresting": score,
            }

            closed = result.get("closed_result", {})
            _insert_run(
                conn,
                iteration=iteration,
                seed_id=seed.seed_id,
                seed_text=seed.text,
                mutated_input=mutated_text,
                status=closed.get("status"),
                bug_signature=closed.get("bug_signature"),
                isinteresting_score=score,
                target=effective_target,
            )
            _insert_seen_branches(db_path, result)

            if score > 0.5 and mutated_text not in added_seed_inputs:
                candidate = _make_discovered_seed(
                    mutated_text, family, seed.bucket, next_discovered_ordinal
                )
                scheduler.add(candidate, metadata={"bucket": candidate.bucket})
                added_seed_inputs.add(mutated_text)
                next_discovered_ordinal += 1

            if iteration:
                closed = result.get("closed_result", {})
                status = closed.get("status")
                print(
                    f"[iter {iteration}] input={mutated_text} target={effective_target} "
                    f"seed={seed.seed_id} bucket={seed.bucket} status={status} "
                    f"score={score:.3f}"
                )
            iteration += 1
            if remaining is not None:
                remaining -= 1

        if batch_scores:
            avg_score = sum(batch_scores) / len(batch_scores)
            scheduler.update(
                scheduled,
                isinteresting_score=avg_score,
                signals=last_signals,
            )

    conn.close()
    _export_results(
        results_folder=results_folder,
        db_path=db_path,
        target=effective_target,
    )


def main() -> None:
    config = build_config()
    run_fuzzer(config)


if __name__ == "__main__":
    if current_process().name == "MainProcess":
        main()
