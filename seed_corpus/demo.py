from __future__ import annotations

import random
import sys
from pathlib import Path

if __package__ in (None, ""):
    # Support `python demo.py` when run from inside `seed_corpus/`.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from seed_corpus import SeedCorpus, corpus_summary_text


def main() -> None:
    corpus = SeedCorpus.load()
    print("Corpus summary")
    print(corpus_summary_text(corpus))
    print()
    print("Targets / families")
    print(" ", corpus.families())
    print(" ", corpus.target("json-decoder"))
    print(" ", corpus.target("ipv4-parser"))
    print(" ", corpus.target("ipv6-parser"))
    print()

    print("Example 1: single-seed deterministic draws (json-decoder)")
    rng = random.Random(1337)
    for _ in range(5):
        seed = corpus.sample("json-decoder", rng=rng)
        print(f"  {seed.seed_id} [{seed.bucket}] -> {seed.label}")
    print()

    print("Example 2: exact-ratio batch for json-decoder (fits capacity)")
    rng = random.Random(2026)
    json_batch = corpus.sample_ratio_batch(
        "json-decoder",
        total=40,
        bucket_ratios={"valid": 0.5, "string_stress": 0.25, "near_valid": 0.25},
        rng=rng,
        shuffle=True,
    )
    json_counts = {"valid": 0, "string_stress": 0, "near_valid": 0}
    for seed in json_batch:
        json_counts[seed.bucket] += 1
    print(f"  sampled 40 -> {json_counts}")
    print()

    print("Example 3: exact 70/20/10 batch for cidrize-runner (pools ipv4 + ipv6)")
    rng = random.Random(42)
    cidr_batch = corpus.sample_ratio_batch(
        "cidrize-runner",
        total=50,
        bucket_ratios={"valid": 0.7, "string_stress": 0.2, "near_valid": 0.1},
        rng=rng,
        shuffle=True,
    )
    bucket_counts = {"valid": 0, "string_stress": 0, "near_valid": 0}
    family_counts = {"ipv4": 0, "ipv6": 0}
    for seed in cidr_batch:
        bucket_counts[seed.bucket] += 1
        family_counts[seed.family] += 1
    print(f"  buckets={bucket_counts}")
    print(f"  families={family_counts}")
    print()

    print("Example 4: capacity error (expected)")
    try:
        corpus.sample_ratio_batch(
            "json-decoder",
            total=50,
            bucket_ratios={"valid": 0.7, "string_stress": 0.2, "near_valid": 0.1},
            rng=random.Random(1),
        )
    except ValueError as exc:
        print(f"  {exc}")

if __name__ == "__main__":
    main()
