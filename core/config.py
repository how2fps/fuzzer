from __future__ import annotations

import argparse
import sys
from typing import TypedDict

from isinteresting import list_versions as isinteresting_versions
from mutator import list_versions as mutator_versions
from parser import DEFAULT_TIMEOUT, TARGETS, list_versions as parser_versions
from power_scheduler import list_versions as power_scheduler_versions
from seed_corpus import list_versions as seed_corpus_versions
from seed_scheduler import list_versions as scheduler_versions


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


def build_config() -> FuzzConfig:
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

    args = parser.parse_args()

    if args.max_hours is not None and "--iterations" in sys.argv:
        parser.error(
            "Cannot specify both --iterations and --hours; use exactly one.")
    if args.max_hours is not None:
        if args.max_hours <= 0:
            parser.error("--hours must be positive.")
    if args.seed_preload_total <= 0:
        parser.error("--seed-preload-total must be positive.")

    max_iterations: int | None = None if args.max_hours is not None else args.max_iterations
    return {
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


def print_config(config: FuzzConfig) -> None:
    print("Fuzzer configuration:")
    print(f"  target: {config['target']}")
    print(f"  scheduler_kind: {config['scheduler_kind']}")
    print(f"  mutator_kind: {config['mutator_kind']}")
    print(f"  ucb_trace: {config['ucb_trace']}")
    print(f"  ucb_debug_tree: {config['ucb_debug_tree']}")
    print(f"  max_iterations: {config['max_iterations']}")
    print(f"  max_hours: {config['max_hours']}")
    print(f"  timeout: {config['timeout']}")
    print(f"  rng_seed: {config['rng_seed']}")
    print(f"  workers: {config['workers']}")
    print(f"  isinteresting_version: {config['isinteresting_version']}")
    print(f"  mutator_version: {config['mutator_version']}")
    print(f"  parser_version: {config['parser_version']}")
    print(f"  power_scheduler_version: {config['power_scheduler_version']}")
    print(f"  seed_corpus_version: {config['seed_corpus_version']}")

