from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import TypedDict

from isinteresting import list_versions as isinteresting_versions
from mutator import list_versions as mutator_versions
from parser import (
    DEFAULT_TIMEOUT,
    TARGETS,
    get_target_registry,
    list_versions as parser_versions,
)
from power_scheduler import list_versions as power_scheduler_versions
from seed_corpus import canonicalize_version as canonicalize_seed_corpus_version
from seed_corpus import list_versions as seed_corpus_versions
from seed_scheduler import list_versions as scheduler_versions

from core.mutation_utils import DEFAULT_PRELOAD_BUCKET_RATIOS

ENABLE_OPEN_COVERAGE: bool = False
BATCH_CONFIG_KEYS = {"runs"}
CONFIG_MODULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("target", ("target",)),
    (
        "runtime",
        (
            "debug_mode",
            "max_iterations",
            "max_hours",
            "timeout",
            "memory_telemetry_seconds",
            "worker_max_jobs",
            "rng_seed",
            "workers",
        ),
    ),
    (
        "seed_scheduler",
        (
            "scheduler_kind",
            "ucb_trace",
            "ucb_debug_tree",
            "heap_startup_min_batches_per_seed",
        ),
    ),
    (
        "seed_corpus",
        (
            "seed_corpus_version",
            "seed_corpus_initial_draw",
            "seed_preload_mode",
            "seed_preload_total",
            "seed_preload_bucket_ratios",
            "seed_refill_mode",
            "llm_seed_candidates",
            "run_startup_generated_unmutated_first",
        ),
    ),
    (
        "mutator",
        (
            "mutator_kind",
            "mutator_version",
            "mutation_chain_continue_probability",
            "mutation_chain_max_depth",
            "grammar_path",
            "ast_grammar_path",
            "grammar_rules_file",
        ),
    ),
    ("isinteresting", ("isinteresting_version",)),
    (
        "parser",
        (
            "parser_version",
            "parser_config",
            "enable_open_coverage",
            "enable_qemu_coverage",
        ),
    ),
    ("power_scheduler", ("power_scheduler_version",)),
)
CONFIG_KEYS = {key for _, keys in CONFIG_MODULES for key in keys}
NESTED_CONFIG_MODULE_NAMES = {
    module_name for module_name, _ in CONFIG_MODULES if module_name != "target"
}


class FuzzConfig(TypedDict):
    target: str

    debug_mode: bool
    max_iterations: int | None
    max_hours: float | None
    timeout: float
    memory_telemetry_seconds: float
    worker_max_jobs: int
    rng_seed: int | None
    workers: int

    scheduler_kind: str
    ucb_trace: bool
    ucb_debug_tree: bool
    heap_startup_min_batches_per_seed: int

    seed_corpus_version: str
    seed_corpus_initial_draw: str | None
    seed_preload_mode: str
    seed_preload_total: int
    seed_preload_bucket_ratios: dict[str, float]
    seed_refill_mode: str
    llm_seed_candidates: int
    run_startup_generated_unmutated_first: bool

    mutator_kind: str
    mutator_version: str
    mutation_chain_continue_probability: float
    mutation_chain_max_depth: int
    grammar_path: str | None
    ast_grammar_path: str | None
    grammar_rules_file: str | None

    isinteresting_version: str
    parser_version: str
    parser_config: dict[str, object]
    enable_open_coverage: bool
    enable_qemu_coverage: bool

    power_scheduler_version: str


def get_default_config() -> FuzzConfig:
    """Return a FuzzConfig with all default values (for merging with file config)."""
    return {
        "target": "json-decoder",
        "debug_mode": False,
        "max_iterations": 10,
        "max_hours": None,
        "timeout": DEFAULT_TIMEOUT,
        "memory_telemetry_seconds": 0.0,
        "worker_max_jobs": 500,
        "rng_seed": None,
        "workers": 1,
        "scheduler_kind": "heap",
        "ucb_trace": False,
        "ucb_debug_tree": False,
        "heap_startup_min_batches_per_seed": 1,
        "seed_corpus_version": "base",
        "seed_corpus_initial_draw": None,
        "seed_preload_mode": "full",
        "seed_preload_total": 8,
        "seed_preload_bucket_ratios": dict(DEFAULT_PRELOAD_BUCKET_RATIOS),
        "seed_refill_mode": "historical",
        "llm_seed_candidates": 5,
        "run_startup_generated_unmutated_first": False,
        "mutator_kind": "auto",
        "mutator_version": "base",
        "mutation_chain_continue_probability": 0.2,
        "mutation_chain_max_depth": 8,
        "grammar_path": None,
        "ast_grammar_path": None,
        "grammar_rules_file": None,
        "isinteresting_version": "base",
        "parser_version": "base",
        "parser_config": {},
        "enable_open_coverage": ENABLE_OPEN_COVERAGE,
        "enable_qemu_coverage": False,
        "power_scheduler_version": "base",
    }


def _deep_merge_dicts(base: dict[str, object], override: dict[str, object]) -> dict[str, object]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dicts(merged[key], value)  # type: ignore[arg-type]
            continue
        merged[key] = copy.deepcopy(value)
    return merged


def _merge_config_values(
    base: dict[str, object],
    override: dict[str, object],
) -> dict[str, object]:
    for key, value in override.items():
        if key not in CONFIG_KEYS or value is None:
            continue
        if (
            key == "parser_config"
            and isinstance(base.get(key), dict)
            and isinstance(value, dict)
        ):
            base[key] = _deep_merge_dicts(base[key], value)  # type: ignore[arg-type]
            continue
        base[key] = copy.deepcopy(value)
    return base


CLI_OVERRIDE_FLAGS: dict[str, tuple[str, ...]] = {
    "target": ("--target",),
    "scheduler_kind": ("--scheduler",),
    "heap_startup_min_batches_per_seed": ("--heap-startup-min-batches-per-seed",),
    "mutator_kind": ("--mutator",),
    "mutation_chain_continue_probability": ("--mutation-chain-continue-probability",),
    "mutation_chain_max_depth": ("--mutation-chain-max-depth",),
    "grammar_path": ("--grammar-file",),
    "ast_grammar_path": ("--ast-grammar-file",),
    "grammar_rules_file": ("-g", "--grammar-rules-file"),
    "debug_mode": ("--debug",),
    "seed_preload_mode": ("--seed-preload-mode",),
    "seed_preload_total": ("--seed-preload-total",),
    "seed_refill_mode": ("--seed-refill-mode",),
    "ucb_trace": ("--ucb-trace",),
    "ucb_debug_tree": ("--ucb-debug-tree",),
    "max_iterations": ("--iterations",),
    "max_hours": ("--hours",),
    "timeout": ("--timeout",),
    "memory_telemetry_seconds": ("--memory-telemetry-seconds",),
    "worker_max_jobs": ("--worker-max-jobs",),
    "rng_seed": ("--seed",),
    "workers": ("--workers",),
    "isinteresting_version": ("--isinteresting-version",),
    "mutator_version": ("--mutator-version",),
    "parser_version": ("--parser-version",),
    "power_scheduler_version": ("--power-scheduler-version",),
    "seed_corpus_version": ("--seed-corpus-version",),
    "llm_seed_candidates": ("--llm-seed-candidates",),
    "enable_open_coverage": ("--enable-open-coverage",),
    "enable_qemu_coverage": ("--enable-qemu-coverage",),
}


def _cli_option_present(argv: list[str], option_strings: tuple[str, ...]) -> bool:
    return any(
        arg == option or arg.startswith(f"{option}=")
        for arg in argv
        for option in option_strings
    )


def _get_cli_override_keys(argv: list[str]) -> set[str]:
    return {
        key
        for key, option_strings in CLI_OVERRIDE_FLAGS.items()
        if _cli_option_present(argv, option_strings)
    }


def _apply_cli_overrides(
    config: FuzzConfig,
    cli_values: FuzzConfig,
    override_keys: set[str],
) -> FuzzConfig:
    merged: FuzzConfig = copy.deepcopy(config)
    _merge_config_values(merged, {key: cli_values[key] for key in override_keys})

    if "seed_preload_mode" in override_keys:
        merged["seed_corpus_initial_draw"] = None
    if "max_hours" in override_keys:
        merged["max_iterations"] = None
    elif "max_iterations" in override_keys:
        merged["max_hours"] = None

    merged["seed_corpus_version"] = canonicalize_seed_corpus_version(
        merged["seed_corpus_version"]
    )
    _validate_config(merged)
    return merged


def _normalize_config_data(data: object) -> dict[str, object]:
    if not isinstance(data, dict):
        raise ValueError("Config file must contain a JSON object.")

    normalized: dict[str, object] = {}
    _merge_config_values(normalized, data)
    for module_name in NESTED_CONFIG_MODULE_NAMES:
        module_config = data.get(module_name)
        if module_config is None:
            continue
        if not isinstance(module_config, dict):
            raise ValueError(f"{module_name} must be an object when provided.")
        _merge_config_values(normalized, module_config)
    return normalized


def _load_config_file_data(path: Path) -> dict[str, object]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("Config file must contain a JSON object.")
    return data


def _extract_runs_per_config(data: dict[str, object]) -> int | None:
    raw_runs = data.get("runs")
    batch = data.get("batch")
    if isinstance(batch, dict) and "runs" in batch:
        raw_runs = batch.get("runs")
    if raw_runs is None:
        return None
    if not isinstance(raw_runs, int) or raw_runs < 1:
        raise ValueError("batch.runs (or top-level runs) must be an integer >= 1.")
    return raw_runs


def _iter_grouped_config_values(config: FuzzConfig) -> list[tuple[str, dict[str, object]]]:
    grouped: list[tuple[str, dict[str, object]]] = []
    for module_name, keys in CONFIG_MODULES:
        grouped.append(
            (
                module_name,
                {key: copy.deepcopy(config[key]) for key in keys},
            )
        )
    return grouped


def _validate_config(config: FuzzConfig) -> None:
    """Validate config values against allowed choices; raise ValueError on invalid."""
    parser_config = config["parser_config"]
    if not isinstance(parser_config, dict):
        raise ValueError("parser_config must be an object.")
    parser_targets = parser_config.get("targets")
    if parser_targets is not None and not isinstance(parser_targets, dict):
        raise ValueError("parser_config.targets must be an object when provided.")
    parser_targets_base_dir = parser_config.get("targets_base_dir")
    if parser_targets_base_dir is not None and (
        not isinstance(parser_targets_base_dir, str) or not parser_targets_base_dir.strip()
    ):
        raise ValueError("parser_config.targets_base_dir must be a non-empty string when provided.")
    if isinstance(parser_targets, dict):
        for target_name, entry in parser_targets.items():
            if not isinstance(target_name, str) or not target_name.strip():
                raise ValueError("parser_config.targets keys must be non-empty strings.")
            if not isinstance(entry, dict):
                raise ValueError(f"parser_config.targets[{target_name!r}] must be an object.")
            command = entry.get("command")
            if command is not None and not isinstance(command, dict):
                raise ValueError(
                    f"parser_config.targets[{target_name!r}].command must be an object."
                )
            coverage = entry.get("coverage")
            if coverage is not None and not isinstance(coverage, dict):
                raise ValueError(
                    f"parser_config.targets[{target_name!r}].coverage must be an object."
                )

    available_targets = get_target_registry(
        parser_config=config["parser_config"]  # type: ignore[arg-type]
    )
    if config["target"] not in available_targets:
        raise ValueError(
            f"Invalid target: {config['target']}. Must be one of: {sorted(available_targets.keys())}"
        )
    scheduler_choices = list(scheduler_versions())
    if config["scheduler_kind"] not in scheduler_choices:
        raise ValueError(
            f"Invalid scheduler_kind: {config['scheduler_kind']}. Must be one of: {scheduler_choices}"
        )
    if config["heap_startup_min_batches_per_seed"] < 0:
        raise ValueError("heap_startup_min_batches_per_seed must be >= 0.")
    if config["mutator_kind"] != "auto":
        raise ValueError(
            f"Invalid mutator_kind: {config['mutator_kind']}. Must be auto."
        )
    if not isinstance(config["mutation_chain_continue_probability"], (int, float)):
        raise ValueError("mutation_chain_continue_probability must be a number.")
    if not 0.0 <= float(config["mutation_chain_continue_probability"]) < 1.0:
        raise ValueError(
            "mutation_chain_continue_probability must be in [0.0, 1.0)."
        )
    if not isinstance(config["mutation_chain_max_depth"], int):
        raise ValueError("mutation_chain_max_depth must be an integer.")
    if config["mutation_chain_max_depth"] < 1:
        raise ValueError("mutation_chain_max_depth must be >= 1.")
    grammar_path = config["grammar_path"]
    if grammar_path is not None:
        if not isinstance(grammar_path, str) or not grammar_path.strip():
            raise ValueError("grammar_path must be a non-empty string or null.")
        if not Path(grammar_path).is_file():
            raise ValueError(f"grammar_path does not exist: {grammar_path}")
    ast_grammar_path = config["ast_grammar_path"]
    if ast_grammar_path is not None:
        if not isinstance(ast_grammar_path, str) or not ast_grammar_path.strip():
            raise ValueError("ast_grammar_path must be a non-empty string or null.")
        if not Path(ast_grammar_path).is_file():
            raise ValueError(f"ast_grammar_path does not exist: {ast_grammar_path}")
    if config["seed_preload_mode"] not in ("full", "ratio_batch", "sample"):
        raise ValueError(
            f"Invalid seed_preload_mode: {config['seed_preload_mode']}. "
            "Must be full, ratio_batch, or sample."
        )
    draw = config["seed_corpus_initial_draw"]
    if draw is not None and draw not in ("bucketed", "random", "full"):
        raise ValueError(
            f"Invalid seed_corpus_initial_draw: {draw!r}. "
            "Must be null, bucketed, random, or full."
        )
    if config["seed_preload_total"] < 0:
        raise ValueError("seed_preload_total must be >= 0.")
    ratios = config["seed_preload_bucket_ratios"]
    if not isinstance(ratios, dict) or not ratios:
        raise ValueError("seed_preload_bucket_ratios must be a non-empty object.")
    for name, weight in ratios.items():
        if not isinstance(name, str) or not name:
            raise ValueError("seed_preload_bucket_ratios keys must be non-empty strings.")
        if not isinstance(weight, (int, float)) or weight < 0:
            raise ValueError(
                f"seed_preload_bucket_ratios[{name!r}] must be a non-negative number."
            )
    if sum(float(v) for v in ratios.values()) <= 0:
        raise ValueError("sum of seed_preload_bucket_ratios values must be > 0.")
    if config["seed_refill_mode"] not in ("historical", "grammar"):
        raise ValueError(
            f"Invalid seed_refill_mode: {config['seed_refill_mode']}. "
            "Must be historical or grammar."
        )
    if config["max_hours"] is not None and config["max_iterations"] is not None:
        raise ValueError("Cannot set both max_iterations and max_hours.")
    if config["max_hours"] is not None and config["max_hours"] <= 0:
        raise ValueError("max_hours must be positive.")
    if config["memory_telemetry_seconds"] < 0:
        raise ValueError("memory_telemetry_seconds must be >= 0.")
    if config["worker_max_jobs"] < 0:
        raise ValueError("worker_max_jobs must be >= 0.")
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
    grammar_rules_file = config["grammar_rules_file"]
    if grammar_rules_file is not None and not Path(grammar_rules_file).is_file():
        raise ValueError(f"grammar_rules_file does not exist: {grammar_rules_file}")
    if config["llm_seed_candidates"] < 0:
        raise ValueError("llm_seed_candidates must be >= 0.")
    if not isinstance(config["run_startup_generated_unmutated_first"], bool):
        raise ValueError("run_startup_generated_unmutated_first must be a boolean.")
    if not isinstance(config["enable_open_coverage"], bool):
        raise ValueError("enable_open_coverage must be a boolean.")
    if not isinstance(config["enable_qemu_coverage"], bool):
        raise ValueError("enable_qemu_coverage must be a boolean.")


def load_config_from_file(path: Path) -> FuzzConfig:
    """Load and validate FuzzConfig from a JSON file. Missing keys use defaults."""
    data = _load_config_file_data(path)
    defaults = get_default_config()
    merged: FuzzConfig = {**defaults}
    normalized_data = _normalize_config_data(data)
    _merge_config_values(merged, normalized_data)
    if merged["mutator_kind"] in {"json", "ip", "grammar"}:
        merged["mutator_kind"] = "auto"
    if merged["grammar_path"] is not None:
        grammar_path = Path(merged["grammar_path"])
        if not grammar_path.is_absolute():
            grammar_path = (path.parent / grammar_path).resolve()
        merged["grammar_path"] = str(grammar_path)
    if merged["ast_grammar_path"] is not None:
        ast_grammar_path = Path(merged["ast_grammar_path"])
        if not ast_grammar_path.is_absolute():
            ast_grammar_path = (path.parent / ast_grammar_path).resolve()
        merged["ast_grammar_path"] = str(ast_grammar_path)
    grammar_rules_file = merged["grammar_rules_file"]
    if grammar_rules_file is not None:
        grammar_rules_path = Path(grammar_rules_file)
        if not grammar_rules_path.is_absolute():
            grammar_rules_path = (path.parent / grammar_rules_path).resolve()
        merged["grammar_rules_file"] = str(grammar_rules_path)
    parser_config = merged["parser_config"]
    if isinstance(parser_config, dict):
        raw_targets_base_dir = parser_config.get("targets_base_dir")
        if isinstance(raw_targets_base_dir, str) and raw_targets_base_dir.strip():
            targets_base_dir = Path(raw_targets_base_dir)
            if not targets_base_dir.is_absolute():
                targets_base_dir = (path.parent / targets_base_dir).resolve()
            parser_config["targets_base_dir"] = str(targets_base_dir)
    merged["seed_corpus_version"] = canonicalize_seed_corpus_version(
        merged["seed_corpus_version"]
    )
    if merged.get("max_hours"):
        merged["max_iterations"] = None
    if merged["seed_corpus_initial_draw"] is not None:
        _draw_map = {
            "bucketed": "ratio_batch",
            "random": "sample",
            "full": "full",
        }
        merged["seed_preload_mode"] = _draw_map[merged["seed_corpus_initial_draw"]]
    _validate_config(merged)
    return merged


def load_runs_per_config_from_file(path: Path) -> int | None:
    """Load optional plan-level run repetition count from a JSON config file."""
    data = _load_config_file_data(path)
    return _extract_runs_per_config(data)


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
    entries = get_run_plan()
    if len(entries) != 1:
        raise RuntimeError(
            "build_config() expects a single run; use get_run_plan() when using --config or --configs-dir."
        )
    return entries[0][1]


def get_run_plan() -> list[tuple[Path | None, FuzzConfig, int]]:
    """
    Parse CLI and return a list of (config_path_or_None, config, runs_per_config).
    Use --config for one file, --configs-dir to run all configs in a folder, --runs to override repeat count.
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
        "--heap-startup-min-batches-per-seed",
        dest="heap_startup_min_batches_per_seed",
        type=int,
        default=1,
        help=(
            "For heap scheduling, guarantee this many leased batches for each "
            "startup-preloaded seed before score-based reprioritization takes over."
        ),
    )
    parser.add_argument(
        "--mutator",
        dest="mutator_kind",
        default="auto",
        choices=["auto"],
        help="Mutation mode. Auto resolves to grammar-driven mutation using the supplied grammar file or built-in target grammar.",
    )
    parser.add_argument(
        "--grammar-file",
        dest="grammar_path",
        type=Path,
        default=None,
        metavar="PATH",
        help="Optional grammar JSON file used by grammar-driven mutators for the active target.",
    )
    parser.add_argument(
        "--ast-grammar-file",
        dest="ast_grammar_path",
        type=Path,
        default=None,
        metavar="PATH",
        help="Optional AST grammar JSON file used by the grammar_ast mutator for the active target.",
    )
    parser.add_argument(
        "-g",
        "--grammar-rules-file",
        dest="grammar_rules_file",
        type=Path,
        default=None,
        metavar="PATH",
        help="Optional text file with extra grammar rules for the grammar_ast mutator.",
    )
    parser.add_argument(
        "--debug",
        dest="debug_mode",
        action="store_true",
        help="Run with the old verbose debug output instead of the live Rich table.",
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
        default=8,
        help="Number of startup seeds to preload when using `ratio_batch` or `sample` mode.",
    )
    parser.add_argument(
        "--seed-refill-mode",
        default="historical",
        choices=["historical", "grammar"],
        help=(
            "How to replenish scheduler seeds at runtime: the existing history-based "
            "refill (`historical`) or grammar-coverage-directed refill (`grammar`)."
        ),
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
        "--memory-telemetry-seconds",
        dest="memory_telemetry_seconds",
        type=float,
        default=0.0,
        help=(
            "Log coordinator+worker RSS every N seconds (0 disables). "
            "Useful for quantifying memory pressure on VPS runs."
        ),
    )
    parser.add_argument(
        "--worker-max-jobs",
        dest="worker_max_jobs",
        type=int,
        default=500,
        help=(
            "Recycle a worker process after N completed jobs (0 disables recycling). "
            "Useful when parser-side memory grows in long runs."
        ),
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
        "--mutation-chain-continue-probability",
        dest="mutation_chain_continue_probability",
        type=float,
        default=0.2,
        help=(
            "After the first mutation, probability of mutating the result again. "
            "This creates an exponentially decreasing chance of deeper mutation chains."
        ),
    )
    parser.add_argument(
        "--mutation-chain-max-depth",
        dest="mutation_chain_max_depth",
        type=int,
        default=8,
        help=(
            "Maximum number of mutation rounds applied when building a single candidate."
        ),
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
        "--llm-seed-candidates",
        type=int,
        default=5,
        help="Number of candidate seeds requested from the LLM generator.",
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
        default=None,
        metavar="DIR",
        help="Run all .json configs in DIR (non-recursive). Each is run --runs times.",
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
    parser.add_argument(
        "--enable-qemu-coverage",
        action="store_true",
        default=False,
        help=(
            "Enable optional AFL-QEMU basic-block bitmap collection for supported "
            "closed/binary targets."
        ),
    )

    argv = sys.argv[1:]
    args = parser.parse_args()
    cli_override_keys = _get_cli_override_keys(argv)
    runs_override_present = _cli_option_present(argv, ("--runs",))

    if args.max_hours is not None and "--iterations" in argv:
        parser.error(
            "Cannot specify both --iterations and --hours; use exactly one.")
    if args.max_hours is not None:
        if args.max_hours <= 0:
            parser.error("--hours must be positive.")
    if args.seed_preload_total < 0:
        parser.error("--seed-preload-total must be >= 0.")
    if args.heap_startup_min_batches_per_seed < 0:
        parser.error("--heap-startup-min-batches-per-seed must be >= 0.")
    if not 0.0 <= args.mutation_chain_continue_probability < 1.0:
        parser.error("--mutation-chain-continue-probability must be in [0.0, 1.0).")
    if args.mutation_chain_max_depth < 1:
        parser.error("--mutation-chain-max-depth must be >= 1.")
    if args.memory_telemetry_seconds < 0:
        parser.error("--memory-telemetry-seconds must be >= 0.")
    if args.worker_max_jobs < 0:
        parser.error("--worker-max-jobs must be >= 0.")
    if args.config is not None and args.configs_dir is not None:
        parser.error("Cannot specify both --config and --configs-dir.")
    if args.runs < 1:
        parser.error("--runs must be at least 1.")
    if args.config is not None and not args.config.is_file():
        parser.error(f"--config path is not a file: {args.config}")
    if args.configs_dir is not None and not args.configs_dir.is_dir():
        parser.error(f"--configs-dir is not a directory: {args.configs_dir}")
    if args.grammar_path is not None and not args.grammar_path.is_file():
        parser.error(f"--grammar-file path is not a file: {args.grammar_path}")
    if args.ast_grammar_path is not None and not args.ast_grammar_path.is_file():
        parser.error(f"--ast-grammar-file path is not a file: {args.ast_grammar_path}")
    if args.grammar_rules_file is not None and not args.grammar_rules_file.is_file():
        parser.error(f"--grammar-rules-file path is not a file: {args.grammar_rules_file}")

    max_iterations: int | None = None if args.max_hours is not None else args.max_iterations
    from_args: FuzzConfig = {
        "target": args.target,
        "scheduler_kind": args.scheduler_kind,
        "heap_startup_min_batches_per_seed": args.heap_startup_min_batches_per_seed,
        "mutator_kind": args.mutator_kind,
        "mutation_chain_continue_probability": args.mutation_chain_continue_probability,
        "mutation_chain_max_depth": args.mutation_chain_max_depth,
        "grammar_path": (
            str(args.grammar_path.resolve())
            if args.grammar_path is not None
            else None
        ),
        "ast_grammar_path": (
            str(args.ast_grammar_path.resolve())
            if args.ast_grammar_path is not None
            else None
        ),
        "debug_mode": args.debug_mode,
        "seed_preload_mode": args.seed_preload_mode,
        "seed_preload_total": args.seed_preload_total,
        "seed_refill_mode": args.seed_refill_mode,
        "ucb_trace": args.ucb_trace,
        "ucb_debug_tree": args.ucb_debug_tree,
        "max_iterations": max_iterations,
        "max_hours": args.max_hours,
        "timeout": args.timeout,
        "memory_telemetry_seconds": args.memory_telemetry_seconds,
        "worker_max_jobs": args.worker_max_jobs,
        "rng_seed": args.rng_seed,
        "workers": args.workers,
        "isinteresting_version": args.isinteresting_version,
        "mutator_version": args.mutator_version,
        "parser_version": args.parser_version,
        "power_scheduler_version": args.power_scheduler_version,
        "seed_corpus_version": args.seed_corpus_version,
        "grammar_rules_file": (
            str(args.grammar_rules_file.resolve())
            if args.grammar_rules_file is not None
            else None
        ),
        "llm_seed_candidates": args.llm_seed_candidates,
        "run_startup_generated_unmutated_first": False,
        "enable_open_coverage": args.enable_open_coverage,
        "enable_qemu_coverage": args.enable_qemu_coverage,
        "parser_config": {},
        "seed_preload_bucket_ratios": dict(DEFAULT_PRELOAD_BUCKET_RATIOS),
        "seed_corpus_initial_draw": None,
    }
    from_args["seed_corpus_version"] = canonicalize_seed_corpus_version(
        from_args["seed_corpus_version"]
    )

    if args.config is not None:
        file_runs = load_runs_per_config_from_file(args.config)
        config = _apply_cli_overrides(
            load_config_from_file(args.config),
            from_args,
            cli_override_keys,
        )
        runs_per_config = args.runs if runs_override_present else (file_runs or 1)
        return [(args.config, config, runs_per_config)]
    if args.configs_dir is not None:
        paths = list_config_files(args.configs_dir)
        if not paths:
            parser.error(f"No .json config files found in {args.configs_dir} (skip names starting with _).")
        entries = [
            (
                p,
                _apply_cli_overrides(
                    load_config_from_file(p),
                    from_args,
                    cli_override_keys,
                ),
                args.runs
                if runs_override_present
                else (load_runs_per_config_from_file(p) or 1),
            )
            for p in paths
        ]
        return entries
    return [(None, from_args, 1)]


def infer_mutator_kind(
    *,
    mutator_kind: str,
    target: str,
    grammar_path: str | None = None,
) -> str:
    _ = mutator_kind

    if grammar_path is not None:
        return "grammar"

    target_lower = target.lower()
    if "json" in target_lower:
        return "json"
    if "ipv4" in target_lower or "ipv6" in target_lower or "cidr" in target_lower:
        return "ip"
    return "grammar"


def is_debug_run(config: FuzzConfig) -> bool:
    return bool(
        config["debug_mode"] or config["ucb_trace"] or config["ucb_debug_tree"]
    )


def print_config(config: FuzzConfig) -> None:
    from core.fuzzer_logging import get_fuzzer_logger

    log = get_fuzzer_logger()
    log.info("Fuzzer configuration:")
    for module_name, module_values in _iter_grouped_config_values(config):
        log.info("  %s:", module_name)
        for key, value in module_values.items():
            log.info("    %s: %s", key, value)
