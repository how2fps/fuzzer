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
        **({"priority_mode": "avg_score"} if kind == "heap" else {}),
    )
    for seed in batch:
        scheduler.add(seed)

    print("initial", scheduler.stats())
    for step in range(8):
        item = scheduler.next()
        score = _fake_isinteresting_score(item.seed.bucket, rng)
        scheduler.update(
            item,
            isinteresting_score=score,
            signals={"demo_step": step},
        )
        print(
            f"step={step} item={item.item_id} seed={item.seed.seed_id} "
            f"bucket={item.seed.bucket} score={score} prio={item.priority:.3f}"
        )
    print("final", scheduler.stats())


def main() -> None:
    run_demo("queue")
    run_demo("heap")


if __name__ == "__main__":
    main()
