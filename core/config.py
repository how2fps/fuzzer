from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TypedDict

from isinteresting import list_versions as isinteresting_versions
from mutator import list_versions as mutator_versions
from parser import DEFAULT_TIMEOUT, TARGETS, list_versions as parser_versions
from power_scheduler import list_versions as power_scheduler_versions
from seed_corpus import list_versions as seed_corpus_versions
from seed_scheduler import list_versions as scheduler_versions

from core.paths import CONFIGS_DIR

ENABLE_OPEN_COVERAGE: bool = False


class FuzzConfig(TypedDict):
    target: str
    scheduler_kind: str
    mutator_kind: str
    seed_preload_mode: str
    seed_preload_total: int
    ucb_trace: bool
    ucb_debug_tree: bool
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
    llm_seed_fallback: bool
    llm_seed_stagnation_threshold: int
    llm_seed_min_candidates: int
    llm_seed_max_candidates: int
    enable_open_coverage: bool


def get_default_config() -> FuzzConfig:
    """Return a FuzzConfig with all default values (for merging with file config)."""
    return {
        "target": "json-decoder",
        "scheduler_kind": "heap",
        "mutator_kind": "auto",
        "seed_preload_mode": "full",
        "seed_preload_total": 50,
        "ucb_trace": False,
        "ucb_debug_tree": False,
        "max_iterations": 10,
        "max_hours": None,
        "timeout": DEFAULT_TIMEOUT,
        "rng_seed": None,
        "workers": 1,
        "isinteresting_version": "base",
        "mutator_version": "base",
        "parser_version": "base",
        "power_scheduler_version": "base",
        "seed_corpus_version": "base",
        "llm_seed_fallback": False,
        "llm_seed_stagnation_threshold": 0,
        "llm_seed_min_candidates": 5,
        "llm_seed_max_candidates": 12,
        "enable_open_coverage": ENABLE_OPEN_COVERAGE,
    }


def _validate_config(config: FuzzConfig) -> None:
    """Validate config values against allowed choices; raise ValueError on invalid."""
    if config["target"] not in TARGETS:
        raise ValueError(
            f"Invalid target: {config['target']}. Must be one of: {sorted(TARGETS.keys())}"
        )
    scheduler_choices = list(scheduler_versions())
    if config["scheduler_kind"] not in scheduler_choices:
        raise ValueError(
            f"Invalid scheduler_kind: {config['scheduler_kind']}. Must be one of: {scheduler_choices}"
        )
    if config["mutator_kind"] not in ("auto", "json", "ip"):
        raise ValueError(
            f"Invalid mutator_kind: {config['mutator_kind']}. Must be auto, json, or ip."
        )
    if config["seed_preload_mode"] not in ("full", "ratio_batch", "sample"):
        raise ValueError(
            f"Invalid seed_preload_mode: {config['seed_preload_mode']}. "
            "Must be full, ratio_batch, or sample."
        )
    if config["seed_preload_total"] <= 0:
        raise ValueError("seed_preload_total must be positive.")
    if config["max_hours"] is not None and config["max_iterations"] is not None:
        raise ValueError("Cannot set both max_iterations and max_hours.")
    if config["max_hours"] is not None and config["max_hours"] <= 0:
        raise ValueError("max_hours must be positive.")
    if config["isinteresting_version"] not in list(isinteresting_versions()):
        raise ValueError(
            f"Invalid isinteresting_version: {config['isinteresting_version']}"
        )
    if config["mutator_version"] not in list(mutator_versions()):
        raise ValueError(f"Invalid mutator_version: {config['mutator_version']}")
    if config["parser_version"] not in list(parser_versions()):
        raise ValueError(f"Invalid parser_version: {config['parser_version']}")
    if config["power_scheduler_version"] not in list(power_scheduler_versions()):
        raise ValueError(
            f"Invalid power_scheduler_version: {config['power_scheduler_version']}"
        )
    if config["seed_corpus_version"] not in list(seed_corpus_versions()):
        raise ValueError(
            f"Invalid seed_corpus_version: {config['seed_corpus_version']}"
        )
    if config["llm_seed_stagnation_threshold"] < 0:
        raise ValueError("llm_seed_stagnation_threshold must be >= 0.")
    if config["llm_seed_min_candidates"] < 1:
        raise ValueError("llm_seed_min_candidates must be >= 1.")
    if config["llm_seed_max_candidates"] < config["llm_seed_min_candidates"]:
        raise ValueError(
            "llm_seed_max_candidates must be >= llm_seed_min_candidates."
        )
    if not isinstance(config["enable_open_coverage"], bool):
        raise ValueError("enable_open_coverage must be a boolean.")


def load_config_from_file(path: Path) -> FuzzConfig:
    """Load and validate FuzzConfig from a JSON file. Missing keys use defaults."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    defaults = get_default_config()
    merged: FuzzConfig = {**defaults}
    for key in merged:
        if key in data and data[key] is not None:
            merged[key] = data[key]  # type: ignore[literal-required]
    # Normalize: if max_hours is set, clear max_iterations
    if merged.get("max_hours"):
        merged["max_iterations"] = None
    _validate_config(merged)
    return merged


def list_config_files(configs_dir: Path) -> list[Path]:
    """Return sorted paths to .json config files, excluding names starting with _."""
    if not configs_dir.is_dir():
        return []
    out: list[Path] = []
    for p in configs_dir.iterdir():
        if p.suffix.lower() == ".json" and not p.name.startswith("_"):
            out.append(p)
    return sorted(out)


def build_config() -> FuzzConfig:
    """Return a single config from CLI (for backward compatibility). Use get_run_plan() for config files."""
    entries, _ = get_run_plan()
    if len(entries) != 1:
        raise RuntimeError(
            "build_config() expects a single run; use get_run_plan() when using --config or --configs-dir."
        )
    return entries[0][1]


def get_run_plan() -> tuple[list[tuple[Path | None, FuzzConfig]], int]:
    """
    Parse CLI and return (list of (config_path_or_None, config), runs_per_config).
    Use --config for one file, --configs-dir to run all configs in a folder, --runs for repeat count.
    """
    parser = argparse.ArgumentParser(
        description=(
            "AFL-style fuzzer harness wiring seed corpus, mutator, parser, "
            "interestingness scoring, schedulers, and power scheduling. "
        )
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
        "--seed-preload-mode",
        default="full",
        choices=["full", "ratio_batch", "sample"],
        help=(
            "How to preload seeds into the scheduler: all corpus seeds (`full`), "
            "a ratio-balanced batch (`ratio_batch`), or repeated single-seed sampling (`sample`)."
        ),
    )
    parser.add_argument(
        "--seed-preload-total",
        type=int,
        default=50,
        help="Number of startup seeds to preload when using `ratio_batch` or `sample` mode.",
    )
    parser.add_argument(
        "--ucb-trace",
        action="store_true",
        help="Print UCB raw signals, normalized signals, and computed rewards on update.",
    )
    parser.add_argument(
        "--ucb-debug-tree",
        action="store_true",
        help="Print the UCB tree snapshot after each iteration when using ucb_tree.",
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
    parser.add_argument(
        "--llm-seed-fallback",
        action="store_true",
        help="Enable LLM-based seed regeneration when the scheduler is exhausted or stagnates.",
    )
    parser.add_argument(
        "--llm-seed-stagnation-threshold",
        type=int,
        default=0,
        help="Regenerate seeds after this many non-novel results in a row (0 disables stagnation-triggered fallback).",
    )
    parser.add_argument(
        "--llm-seed-min-candidates",
        type=int,
        default=5,
        help="Minimum number of candidate seeds requested from the LLM fallback.",
    )
    parser.add_argument(
        "--llm-seed-max-candidates",
        type=int,
        default=12,
        help="Maximum number of candidate seeds requested from the LLM fallback.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="Path to a single JSON config file (from configs/ or any path).",
    )
    parser.add_argument(
        "--configs-dir",
        type=Path,
        nargs="?",
        default=None,
        const=CONFIGS_DIR,
        metavar="DIR",
        help="Run all .json configs in DIR (default: configs/). Each is run --runs times.",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="Number of times to run each config (when using --config or --configs-dir).",
    )
    parser.add_argument(
        "--enable-open-coverage",
        action="store_true",
        default=ENABLE_OPEN_COVERAGE,
        help="Enable optional coverage collection for open_result targets.",
    )

    args = parser.parse_args()

    if args.max_hours is not None and "--iterations" in sys.argv:
        parser.error(
            "Cannot specify both --iterations and --hours; use exactly one.")
    if args.max_hours is not None:
        if args.max_hours <= 0:
            parser.error("--hours must be positive.")
    if args.seed_preload_total <= 0:
        parser.error("--seed-preload-total must be positive.")
    if args.config is not None and args.configs_dir is not None:
        parser.error("Cannot specify both --config and --configs-dir.")
    if args.runs < 1:
        parser.error("--runs must be at least 1.")
    if args.config is not None and not args.config.is_file():
        parser.error(f"--config path is not a file: {args.config}")
    if args.configs_dir is not None and not args.configs_dir.is_dir():
        parser.error(f"--configs-dir is not a directory: {args.configs_dir}")

    max_iterations: int | None = None if args.max_hours is not None else args.max_iterations
    from_args: FuzzConfig = {
        "target": args.target,
        "scheduler_kind": args.scheduler_kind,
        "mutator_kind": args.mutator_kind,
        "seed_preload_mode": args.seed_preload_mode,
        "seed_preload_total": args.seed_preload_total,
        "ucb_trace": args.ucb_trace,
        "ucb_debug_tree": args.ucb_debug_tree,
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
        "llm_seed_fallback": args.llm_seed_fallback,
        "llm_seed_stagnation_threshold": args.llm_seed_stagnation_threshold,
        "llm_seed_min_candidates": args.llm_seed_min_candidates,
        "llm_seed_max_candidates": args.llm_seed_max_candidates,
        "enable_open_coverage": args.enable_open_coverage,
    }

    if args.config is not None:
        config = load_config_from_file(args.config)
        return ([ (args.config, config) ], args.runs)
    if args.configs_dir is not None:
        paths = list_config_files(args.configs_dir)
        if not paths:
            parser.error(f"No .json config files found in {args.configs_dir} (skip names starting with _).")
        entries = [ (p, load_config_from_file(p)) for p in paths ]
        return (entries, args.runs)
    return ([ (None, from_args) ], 1)


def infer_mutator_kind(*, mutator_kind: str, target: str) -> str:
    if mutator_kind != "auto":
        return mutator_kind

    target_lower = target.lower()
    if "json" in target_lower:
        return "json"
    if "ipv4" in target_lower or "ipv6" in target_lower or "cidr" in target_lower:
        return "ip"
    return "json"


def print_config(config: FuzzConfig) -> None:
    from core.fuzzer_logging import get_fuzzer_logger

    log = get_fuzzer_logger()
    log.info("Fuzzer configuration:")
    log.info("  target: %s", config["target"])
    log.info("  scheduler_kind: %s", config["scheduler_kind"])
    log.info("  mutator_kind: %s", config["mutator_kind"])
    log.info("  ucb_trace: %s", config["ucb_trace"])
    log.info("  ucb_debug_tree: %s", config["ucb_debug_tree"])
    log.info("  max_iterations: %s", config["max_iterations"])
    log.info("  max_hours: %s", config["max_hours"])
    log.info("  timeout: %s", config["timeout"])
    log.info("  rng_seed: %s", config["rng_seed"])
    log.info("  workers: %s", config["workers"])
    log.info("  isinteresting_version: %s", config["isinteresting_version"])
    log.info("  mutator_version: %s", config["mutator_version"])
    log.info("  parser_version: %s", config["parser_version"])
    log.info("  power_scheduler_version: %s", config["power_scheduler_version"])
    log.info("  seed_corpus_version: %s", config["seed_corpus_version"])
    log.info("  llm_seed_fallback: %s", config["llm_seed_fallback"])
    log.info(
        "  llm_seed_stagnation_threshold: %s",
        config["llm_seed_stagnation_threshold"],
    )
    log.info("  llm_seed_min_candidates: %s", config["llm_seed_min_candidates"])
    log.info("  llm_seed_max_candidates: %s", config["llm_seed_max_candidates"])
    log.info("  enable_open_coverage: %s", config["enable_open_coverage"])
