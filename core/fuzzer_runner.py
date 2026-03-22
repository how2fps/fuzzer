from __future__ import annotations

import json
import random
import shutil
import signal
from pathlib import Path
from typing import Any

from core.config import FuzzConfig, infer_mutator_kind
from core.db_utils import init_results_db, warmup_power_schedule
from core.fuzzer_logging import configure_fuzzer_logging, get_fuzzer_logger
from core.sqlite_conn import open_results_db
from isinteresting import get_compute_interestingness
from core.mutation_utils import initial_scheduler_seeds
from mutator import get_mutator
from parser import get_parser
from power_scheduler import get_power_scheduler
from core.results_export import export_results
from seed_corpus import get_corpus_loader
from seed_scheduler import UCBTreeScheduler, make_scheduler
from core.workers import run_fuzzer_multi_worker
from core.target_artifacts import clear_bug_counts_csv


def run_fuzzer(
    config: FuzzConfig,
    *,
    results_folder: Path,
    config_path: Path | None = None,
) -> None:
    configure_fuzzer_logging()
    corpus_loader = get_corpus_loader(config["seed_corpus_version"])
    corpus = corpus_loader.load()

    parser_api = get_parser(config["parser_version"])
    mutate_fn = get_mutator(config["mutator_version"])
    compute_interestingness_fn = get_compute_interestingness(
        config["isinteresting_version"])
    power_scheduler_module = get_power_scheduler(
        config["power_scheduler_version"])

    effective_target = config["target"]
    effective_mutator = infer_mutator_kind(
        mutator_kind=config["mutator_kind"],
        target=effective_target,
    )

    rng = random.Random(
        config["rng_seed"]) if config["rng_seed"] is not None else random.Random()

    scheduler = make_scheduler(config["scheduler_kind"])
    initial_seeds = initial_scheduler_seeds(
        corpus=corpus,
        target=effective_target,
        preload_mode=config["seed_preload_mode"],
        preload_total=config["seed_preload_total"],
        rng=rng,
    )
    for seed in initial_seeds:
        metadata: dict[str, Any] = {
            "bucket": seed.bucket,
            "signals": {
                "coverage_key": {"family": seed.family, "bucket": seed.bucket},
                "status": "ok",
            },
        }
        if config["ucb_trace"] and isinstance(scheduler, UCBTreeScheduler):
            metadata["_ucb_trace"] = True
        scheduler.add(seed, metadata=metadata)

    if not scheduler or scheduler.empty():
        return

    results_folder.mkdir(parents=True, exist_ok=True)
    # Clear canonical target logs + per-worker scratch so bug_counts doesn't leak across runs.
    clear_bug_counts_csv(target=effective_target, results_folder=results_folder)

    # Save the config used for this run into the results folder
    config_dest = results_folder / "config.json"
    if config_path is not None:
        shutil.copy2(config_path, config_dest)
    else:
        with open(config_dest, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

    db_path = results_folder / "runs.db"
    conn = open_results_db(db_path)
    init_results_db(conn)

    seed_energies = warmup_power_schedule(
        corpus=corpus,
        target=effective_target,
        power_scheduler_module=power_scheduler_module,
        conn=conn,
    )

    shutdown_requested: list[bool] = [False]

    def _sigint_handler(_signum: int, _frame: object) -> None:
        shutdown_requested[0] = True
        get_fuzzer_logger().info(
            "\nCtrl+C: shutting down gracefully (workers will finish current run and exit)..."
        )

    original_sigint = None
    try:
        try:
            original_sigint = signal.getsignal(signal.SIGINT)
        except (ValueError, OSError):
            original_sigint = None
        try:
            signal.signal(signal.SIGINT, _sigint_handler)
        except (ValueError, OSError):
            # Some environments (e.g. non-main threads, child processes) do not
            # allow installing signal handlers; in that case we just skip the
            # graceful shutdown behaviour and fall back to defaults.
            pass

        workers = max(1, config["workers"])
        run_fuzzer_multi_worker(
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
    finally:
        try:
            conn.close()
        finally:
            export_results(
                results_folder=results_folder,
                db_path=db_path,
                target=effective_target,
            )
            if original_sigint is not None:
                try:
                    signal.signal(signal.SIGINT, original_sigint)
                except (ValueError, OSError):
                    # If we cannot restore the original handler, ignore; this is
                    # best-effort and should not mask earlier exceptions.
                    pass

