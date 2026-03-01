from __future__ import annotations

import argparse
import csv
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timezone
from multiprocessing import Process, Queue, current_process
from pathlib import Path
from typing import Any, TypedDict

from isinteresting import get_compute_interestingness, list_versions as isinteresting_versions
from mutator import get_mutator, list_versions as mutator_versions
from parser import DEFAULT_TIMEOUT, TARGETS, get_parser, list_versions as parser_versions
from power_scheduler import SeedStats, get_power_scheduler, list_versions as power_scheduler_versions
from seed_corpus import get_corpus_loader, list_versions as seed_corpus_versions
from seed_scheduler import BaseSeedScheduler, get_scheduler, list_versions as scheduler_versions, ScheduledSeed

FUZZER_ROOT = Path(__file__).resolve().parent
RESULTS_DIR = FUZZER_ROOT / "results"
JSON_DECODER_TARGET_DIR = FUZZER_ROOT / "targets" / "json-decoder"
JSON_DECODER_STV_SCRIPT = JSON_DECODER_TARGET_DIR / "json_decoder_stv.py"


class FuzzConfig(TypedDict):
    target: str
    scheduler_kind: str
    mutator_kind: str
    max_iterations: int
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
        help="Maximum number of fuzzing iterations.",
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

    return {
        "target": args.target,
        "scheduler_kind": args.scheduler_kind,
        "mutator_kind": args.mutator_kind,
        "max_iterations": args.max_iterations,
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


def seed_stats_from_corpus(*, corpus: Any, target: str) -> list[SeedStats]:
    target_set = corpus.target(target)
    stats: list[SeedStats] = []
    for seed in target_set.seeds:
        stats.append(
            {
                "id": seed.ordinal,
                "exec_time_ms": None,
                "coverage_bitmap": None,
                "fuzz_count": 0,
            }
        )
    return stats


def warmup_power_schedule(
    *,
    corpus: Any,
    target: str,
    power_scheduler_module: Any,
) -> dict[int, int]:
    stats = seed_stats_from_corpus(corpus=corpus, target=target)
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
    """Run fuzz loop in a worker: request work from coordinator, run one iteration, send result."""
    parser_api = get_parser(config["parser_version"])
    mutate_fn = get_mutator(config["mutator_version"])
    compute_interestingness_fn = get_compute_interestingness(config["isinteresting_version"])
    effective_target = config["target"]
    rng_seed = config["rng_seed"]
    rng = (
        random.Random(rng_seed + worker_id)
        if rng_seed is not None
        else random.Random()
    )
    results_folder = Path(results_folder_str)
    coverage_path: str | None = (
        str((results_folder / f".coverage_w{worker_id}").resolve())
        if effective_target == "json-decoder"
        else None
    )

    while True:
        request_queue.put(1)
        work = reply_queue.get()
        if work is None:
            break

        item_id = work["item_id"]
        iteration = work["iteration"]
        seed_id = work["seed_id"]
        seed_text = work["seed_text"]
        bucket = work["bucket"]

        mutated_text = mutate_fn(
            seed_text,
            mutator_kind=effective_mutator,
            rng=rng,
        )
        result = parser_api.run_parser(
            input_data=mutated_text.encode("utf-8"),
            target=effective_target,
            timeout=config["timeout"],
            print_json=False,
            coverage_file=coverage_path,
        )
        score = compute_interestingness_fn(result=result)
        closed = result.get("closed_result", {})
        signals: dict[str, Any] = {
            "iteration": iteration,
            "seed_id": seed_id,
            "bucket": bucket,
            "status": closed.get("status"),
            "isinteresting": score,
        }
        result_queue.put({
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
    effective_target: str,
    effective_mutator: str,
    results_folder: Path,
    db_path: Path,
    conn: sqlite3.Connection,
    workers: int,
) -> None:
    """Run fuzzer with one shared scheduler in the main process and N workers."""
    request_queue: Queue = Queue()
    reply_queue: Queue = Queue()
    result_queue: Queue = Queue()
    lock = threading.Lock()
    total_jobs: list[int] = [0]
    pending: dict[str, tuple[ScheduledSeed, int]] = {}
    max_iterations = config["max_iterations"]

    def request_handler() -> None:
        nones_sent = 0
        iteration_counter = 0
        while nones_sent < workers:
            request_queue.get()
            with lock:
                if (
                    iteration_counter < max_iterations
                    and not scheduler.empty()
                ):
                    scheduled = scheduler.next()
                    iteration_counter += 1
                    work = {
                        "item_id": scheduled.item_id,
                        "iteration": iteration_counter - 1,
                        "seed_id": scheduled.seed.seed_id,
                        "seed_text": scheduled.seed.text,
                        "bucket": scheduled.seed.bucket,
                    }
                    pending[scheduled.item_id] = (scheduled, iteration_counter - 1)
                    reply_queue.put(work)
                else:
                    if total_jobs[0] == 0:
                        total_jobs[0] = iteration_counter
                    reply_queue.put(None)
                    nones_sent += 1

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
    while True:
        result = result_queue.get()
        with lock:
            item_id = result["item_id"]
            scheduled, iteration = pending.pop(item_id)
            scheduler.update(
                scheduled,
                isinteresting_score=result["isinteresting_score"],
                signals=result["signals"],
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
        results_received += 1
        # if iteration % 100 == 0 or result.get("status") in ("bug", "crash", "timeout"):
        if iteration:
            print(
                f"[iter {iteration}] seed={result['seed_id']} "
                f"score={result['isinteresting_score']:.3f} status={result['status']} mutated input={result['mutated_input']}"
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

    _initial_seed_energies = warmup_power_schedule(
        corpus=corpus,
        target=effective_target,
        power_scheduler_module=power_scheduler_module,
    )

    if not scheduler or scheduler.empty():
        return

    timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    results_folder = RESULTS_DIR / f"{effective_target}_{timestamp_str}"
    results_folder.mkdir(parents=True, exist_ok=True)
    db_path = results_folder / "runs.db"
    conn = sqlite3.connect(str(db_path))
    _init_results_db(conn)

    workers = max(1, config["workers"])
    if workers > 1:
        _run_fuzzer_multi_worker(
            config=config,
            scheduler=scheduler,
            effective_target=effective_target,
            effective_mutator=effective_mutator,
            results_folder=results_folder,
            db_path=db_path,
            conn=conn,
            workers=workers,
        )
        conn.close()
        _export_results(
            results_folder=results_folder,
            db_path=db_path,
            target=effective_target,
        )
        return

    fuzz_counts: dict[str, int] = {}

    for iteration in range(config["max_iterations"]):
        if scheduler.empty():
            break

        scheduled: ScheduledSeed = scheduler.next()
        seed = scheduled.seed

        fuzz_counts[seed.seed_id] = fuzz_counts.get(seed.seed_id, 0) + 1

        mutated_text = mutate_fn(
            seed.text,
            mutator_kind=effective_mutator,
            rng=rng,
        )

        coverage_path = (
            str((results_folder / ".coverage_buggy_json").resolve())
            if effective_target == "json-decoder"
            else None
        )
        result = parser_api.run_parser(
            input_data=mutated_text.encode("utf-8"),
            target=effective_target,
            timeout=config["timeout"],
            print_json=False,
            coverage_file=coverage_path,
        )

        score = compute_interestingness_fn(result=result)

        signals: dict[str, Any] = {
            "iteration": iteration,
            "seed_id": seed.seed_id,
            "bucket": seed.bucket,
            "status": result.get("closed_result", {}).get("status"),
            "isinteresting": score,
        }

        scheduler.update(
            scheduled,
            isinteresting_score=score,
            signals=signals,
        )

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

        # if iteration % 100 == 0:
        if iteration:
            closed = result.get("closed_result", {})
            status = closed.get("status")
            print(
                f"[iter {iteration}] input={mutated_text} target={effective_target} "
                f"seed={seed.seed_id} bucket={seed.bucket} status={status} "
                f"score={score:.3f}"
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
