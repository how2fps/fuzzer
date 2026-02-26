from __future__ import annotations

import random
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from seed_corpus import SeedCorpus
from seed_scheduler import make_scheduler


def _fake_isinteresting_score(seed_bucket: str, rng: random.Random) -> float:
    # Simple demo signal shaping: near_valid tends to be more "interesting".
    base = {"valid": 0.2, "string_stress": 0.5, "near_valid": 0.8}[seed_bucket]
    return round(base + rng.random() * 0.2, 3)


def _demo_signals(step: int, item) -> dict:
    return {
        "coverage_key": f"cov-{step % 3}",
        "bug_key": "NO_BUG" if step % 4 else f"bug-{step}",
        "new_coverage": step % 2 == 0,
        "new_bug": step % 3 == 0,
        "status": "crash" if step == 5 else "ok",
        "source_bucket": item.seed.bucket,
    }


def run_demo(kind: str) -> None:
    print(f"\nScheduler: {kind}")
    corpus = SeedCorpus.load()
    rng = random.Random(42)

    batch = corpus.sample_ratio_batch(
        "cidrize-runner",
        total=20,
        bucket_ratios={"valid": 0.5, "string_stress": 0.25, "near_valid": 0.25},
        rng=rng,
        shuffle=True,
    )
    scheduler = make_scheduler(
        kind,
        **(
            {"priority_mode": "avg_score"}
            if kind == "heap"
            else {"ucb_c": 1.0, "max_seeds_per_leaf": 4}
            if kind == "ucb_tree"
            else {}
        ),
    )
    for i, seed in enumerate(batch):
        if kind == "ucb_tree":
            scheduler.add(
                seed,
                metadata={
                    "signals": {
                        "coverage_key": f"bootstrap-cov-{i % 3}",
                        "bug_key": "NO_BUG" if i % 5 else f"bootstrap-bug-{i}",
                    }
                },
            )
        else:
            scheduler.add(seed)

    print("initial", scheduler.stats())
    for step in range(8):
        item = scheduler.next()
        score = _fake_isinteresting_score(item.seed.bucket, rng)
        signals = _demo_signals(step, item)
        scheduler.update(
            item,
            isinteresting_score=score,
            signals=signals,
        )
        print(
            f"step={step} item={item.item_id} seed={item.seed.seed_id} "
            f"bucket={item.seed.bucket} score={score} prio={item.priority:.3f}"
        )
    print("final", scheduler.stats())


def main() -> None:
    run_demo("queue")
    run_demo("heap")
    run_demo("ucb_tree")


if __name__ == "__main__":
    main()
