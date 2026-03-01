from __future__ import annotations

import argparse
import random
from typing import Any, TypedDict

from isinteresting import compute_interestingness
from mutator import mutate_ip_input, mutate_json_input
from parser import DEFAULT_TIMEOUT, TARGETS, run_parser
from power_scheduler import SeedStats, compute_power_schedule
from seed_corpus import SeedCorpus
from seed_scheduler import BaseSeedScheduler, ScheduledSeed, make_scheduler


class FuzzConfig(TypedDict):
    target: str
    scheduler_kind: str
    mutator_kind: str
    max_iterations: int
    timeout: float
    rng_seed: int | None


def build_config() -> FuzzConfig:
    parser = argparse.ArgumentParser(
        description="AFL-style fuzzer harness wiring seed corpus, mutator, parser, "
        "interestingness scoring, schedulers, and power scheduling."
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
        choices=["queue", "heap", "ucb_tree"],
        help="Seed scheduler kind.",
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
        default=1000,
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

    args = parser.parse_args()

    return {
        "target": args.target,
        "scheduler_kind": args.scheduler_kind,
        "mutator_kind": args.mutator_kind,
        "max_iterations": args.max_iterations,
        "timeout": args.timeout,
        "rng_seed": args.rng_seed,
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


def mutate_seed_text(
    *,
    text: str,
    mutator_kind: str,
    rng: random.Random,
) -> str:
    if mutator_kind == "ip":
        return mutate_ip_input(original_text=text, rng=rng)
    return mutate_json_input(original_text=text, rng=rng)


def seed_stats_from_corpus(*, corpus: SeedCorpus, target: str) -> list[SeedStats]:
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


def warmup_power_schedule(*, corpus: SeedCorpus, target: str) -> dict[int, int]:
    stats = seed_stats_from_corpus(corpus=corpus, target=target)
    if not stats:
        return {}
    schedule = compute_power_schedule(seeds=stats)
    return dict(schedule["seed_energies"])


def init_scheduler(
    *,
    corpus: SeedCorpus,
    target: str,
    scheduler_kind: str,
) -> BaseSeedScheduler:
    scheduler = make_scheduler(scheduler_kind)
    target_set = corpus.target(target)
    for seed in target_set.seeds:
        scheduler.add(seed, metadata={"bucket": seed.bucket})
    return scheduler


def run_fuzzer(config: FuzzConfig) -> None:
    corpus = SeedCorpus.load()
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
    )

    _initial_seed_energies = warmup_power_schedule(
        corpus=corpus,
        target=effective_target,
    )

    if not scheduler or scheduler.empty():
        return

    fuzz_counts: dict[str, int] = {}

    for iteration in range(config["max_iterations"]):
        if scheduler.empty():
            break

        scheduled: ScheduledSeed = scheduler.next()
        seed = scheduled.seed

        fuzz_counts[seed.seed_id] = fuzz_counts.get(seed.seed_id, 0) + 1

        mutated_text = mutate_seed_text(
            text=seed.text,
            mutator_kind=effective_mutator,
            rng=rng,
        )

        result = run_parser(
            input_data=mutated_text.encode("utf-8"),
            target=effective_target,
            timeout=config["timeout"],
            print_json=False,
        )

        score = compute_interestingness(result=result)

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

        # if iteration % 100 == 0:
        if iteration:
            closed = result.get("closed_result", {})
            status = closed.get("status")
            print(
                f"[iter {iteration}] input={mutated_text} target={effective_target} "
                f"seed={seed.seed_id} bucket={seed.bucket} status={status} "
                f"score={score:.3f}"
            )


def main() -> None:
    config = build_config()
    run_fuzzer(config)


if __name__ == "__main__":
    main()
