from __future__ import annotations

import json
import random
import shutil
import signal
from pathlib import Path
from typing import Any

from core.config import FuzzConfig, infer_mutator_kind
from core.db_utils import get_run_summary, init_results_db, warmup_power_schedule
from core.fuzzer_logging import configure_fuzzer_logging, get_fuzzer_logger
from core.live_ui import console, render_run_summary_panel
from core.llm_seed_fallback import make_generated_seed, maybe_generate_seed_candidates
from core.paths import DISCOVERED_SEED_ORDINAL_BASE
from core.sqlite_conn import open_results_db
from core.mutation_utils import initial_scheduler_seeds
from mutator import get_mutator
from power_scheduler import get_power_scheduler
from core.results_export import export_results
from seed_corpus import Seed, get_corpus_loader, get_version_spec
from seed_scheduler import UCBTreeScheduler, make_scheduler
from core.workers import run_fuzzer_multi_worker
from core.target_artifacts import clear_bug_counts_csv
from mutator import configure_runtime_grammar
from mutator.versions import grammar_ast


def run_fuzzer(
    config: FuzzConfig,
    *,
    results_folder: Path,
    config_path: Path | None = None,
) -> None:
    configure_fuzzer_logging()
    log = get_fuzzer_logger()
    corpus_version = get_version_spec(config["seed_corpus_version"])
    use_llm_bootstrap = corpus_version.startup_mode == "llm_bootstrap"
    use_regex_noseed = corpus_version.startup_mode == "grammar_bootstrap"
    corpus_loader = corpus_version.loader
    corpus = corpus_loader.load()
    grammar_ast.configure(grammar_rules_file=config["grammar_rules_file"])

    mutate_fn = get_mutator(config["mutator_version"])
    power_scheduler_module = get_power_scheduler(
        config["power_scheduler_version"])

    effective_target = config["target"]
    effective_mutator = infer_mutator_kind(
        mutator_kind=config["mutator_kind"],
        target=effective_target,
    )
    configure_runtime_grammar(
        kind=effective_mutator,
        grammar_path=config["grammar_path"],
    )

    rng = random.Random(
        config["rng_seed"]) if config["rng_seed"] is not None else random.Random()

    scheduler = make_scheduler(config["scheduler_kind"])
    initial_seeds = (
        []
        if use_llm_bootstrap or use_regex_noseed
        else initial_scheduler_seeds(
            corpus=corpus,
            target=effective_target,
            preload_mode=config["seed_preload_mode"],
            preload_total=config["seed_preload_total"],
            rng=rng,
            bucket_ratios=config["seed_preload_bucket_ratios"],
        )
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

    def _make_regex_seed(*, text: str, family: str, ordinal: int) -> Seed:
        seed_id = f"regex-{family}-{ordinal}"
        return Seed(
            seed_id=seed_id,
            family=family,
            bucket="generated",
            label=seed_id,
            text=text,
            tags=("regex_generated",),
            expected="",
            ordinal=ordinal,
            fingerprint=seed_id,
        )

    results_folder.mkdir(parents=True, exist_ok=True)
    # Clear canonical target logs + per-worker scratch so bug_counts doesn't leak across runs.
    clear_bug_counts_csv(
        target=effective_target,
        results_folder=results_folder,
        parser_config=config.get("parser_config"),  # type: ignore[arg-type]
    )

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
    startup_llm_seeds: list[str] = []
    startup_regex_seeds: list[str] = []

    if scheduler.empty() and use_regex_noseed:
        family = corpus.target(effective_target).family
        requested = max(1, int(config["seed_preload_total"]))
        with console.status(
            f"Generating {requested} grammar seeds for {effective_target}...",
            spinner="dots",
        ):
            generated = grammar_ast.generate_without_seed(
                mutator_kind=effective_mutator,
                rng=rng,
                count=requested,
            )
        startup_regex_seeds = list(generated)
        next_ordinal = DISCOVERED_SEED_ORDINAL_BASE
        for text in generated:
            candidate = _make_regex_seed(
                text=text,
                family=family,
                ordinal=next_ordinal,
            )
            scheduler.add(
                candidate,
                metadata={
                    "bucket": candidate.bucket,
                    "signals": {
                        "coverage_key": {
                            "family": candidate.family,
                            "bucket": candidate.bucket,
                        },
                        "status": "generated",
                    },
                },
            )
            next_ordinal += 1
        if generated:
            log.info(
                "Bootstrapped scheduler with %s grammar-generated seeds.",
                len(generated),
            )

    if scheduler.empty() and use_llm_bootstrap:
        family = corpus.target(effective_target).family
        llm_bootstrap_config = dict(config)
        requested = int(llm_bootstrap_config["llm_seed_candidates"])
        if requested > 0:
            with console.status(
                f"Generating {requested} LLM seeds for {effective_target}...",
                spinner="dots",
            ):
                llm_generated = maybe_generate_seed_candidates(
                    conn=conn,
                    corpus=corpus,
                    target=effective_target,
                    config=llm_bootstrap_config,  # type: ignore[arg-type]
                    results_folder=results_folder,
                    include_corpus_context=not use_llm_bootstrap,
                )
        else:
            llm_generated = None
        if llm_generated is not None and llm_generated.seeds:
            startup_llm_seeds = list(llm_generated.seeds)
            next_ordinal = DISCOVERED_SEED_ORDINAL_BASE
            for text in llm_generated.seeds:
                candidate = make_generated_seed(
                    text=text,
                    family=family,
                    ordinal=next_ordinal,
                )
                scheduler.add(
                    candidate,
                    metadata={
                        "bucket": candidate.bucket,
                        "signals": {
                            "coverage_key": {
                                "family": candidate.family,
                                "bucket": candidate.bucket,
                            },
                            "status": "generated",
                        },
                    },
                )
                next_ordinal += 1
            log.info(
                "Bootstrapped scheduler with %s LLM-generated seeds.",
                len(llm_generated.seeds),
            )

    if not scheduler or scheduler.empty():
        log.warning(
            "No schedulable seeds available after preload%s.",
            (
                " and startup seed bootstrap"
                if use_llm_bootstrap or use_regex_noseed
                else ""
            ),
        )
        return

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
    final_summary_printed = False
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
            startup_generated_seeds=startup_llm_seeds or startup_regex_seeds,
            startup_generated_source=(
                "startup LLM bootstrap"
                if startup_llm_seeds
                else "startup grammar bootstrap"
                if startup_regex_seeds
                else ""
            ),
        )
        console.print(
            render_run_summary_panel(
                target=effective_target,
                results_folder=str(results_folder),
                summary=get_run_summary(conn, target=effective_target),
            )
        )
        final_summary_printed = True
    finally:
        try:
            if not final_summary_printed:
                console.print(
                    render_run_summary_panel(
                        target=effective_target,
                        results_folder=str(results_folder),
                        summary=get_run_summary(conn, target=effective_target),
                    )
                )
            conn.close()
        finally:
            export_results(
                results_folder=results_folder,
                db_path=db_path,
                target=effective_target,
                parser_config=config.get("parser_config"),  # type: ignore[arg-type]
            )
            if original_sigint is not None:
                try:
                    signal.signal(signal.SIGINT, original_sigint)
                except (ValueError, OSError):
                    # If we cannot restore the original handler, ignore; this is
                    # best-effort and should not mask earlier exceptions.
                    pass
