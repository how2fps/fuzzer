from __future__ import annotations

import hashlib
import random
import sqlite3
from typing import Any, Callable

from core.db_utils import input_already_run
from seed_corpus import Seed


DEFAULT_PRELOAD_BUCKET_RATIOS: dict[str, float] = {
    "valid": 0.7,
    "string_stress": 0.2,
    "near_valid": 0.1,
}


def initial_scheduler_seeds(
    *,
    corpus: Any,
    target: str,
    preload_mode: str,
    preload_total: int,
    rng: random.Random,
    bucket_ratios: dict[str, float] | None = None,
) -> list[Seed]:
    if preload_total <= 0:
        return []
    ratios = bucket_ratios if bucket_ratios is not None else DEFAULT_PRELOAD_BUCKET_RATIOS
    if preload_mode == "full":
        return list(corpus.target(target).seeds)
    if preload_mode == "ratio_batch":
        return corpus.sample_ratio_batch(
            target,
            total=preload_total,
            bucket_ratios=ratios,
            rng=rng,
            shuffle=True,
        )
    if preload_mode == "sample":
        return [corpus.sample(target, rng=rng) for _ in range(preload_total)]
    raise ValueError(f"unknown seed preload mode {preload_mode!r}")


def make_discovered_seed(
    mutated_text: str,
    family: str,
    parent_bucket: str,
    ordinal: int,
) -> Seed:
    text_bytes = mutated_text.encode("utf-8")
    fp = hashlib.sha256(text_bytes).hexdigest()[:16]
    seed_id = f"discovered-{fp}"
    return Seed(
        seed_id=seed_id,
        family=family,
        bucket="discovered",
        label=seed_id,
        text=mutated_text,
        tags=(),
        expected="",
        ordinal=ordinal,
        fingerprint=fp,
    )


def generate_unique_mutations(
    n: int,
    seed_text: str,
    mutate_fn: Callable[..., str],
    mutator_kind: str,
    rng: random.Random,
    conn: sqlite3.Connection,
    target: str,
    *,
    max_attempts: int = 200,
) -> list[str]:
    """Generate up to n unique mutated inputs not already present in runs for this target."""
    reject_nul_for_kinds = {"ip", "ipv4", "ipv6"}

    def _is_rejected_candidate(candidate: str) -> bool:
        return mutator_kind in reject_nul_for_kinds and "\x00" in candidate

    seen: set[str] = set()
    batch: list[str] = []
    for _ in range(n):
        candidate = mutate_fn(
            seed_text,
            mutator_kind=mutator_kind,
            rng=rng,
        )
        for _attempt in range(max_attempts):
            if _is_rejected_candidate(candidate):
                candidate = mutate_fn(
                    seed_text,
                    mutator_kind=mutator_kind,
                    rng=rng,
                )
                continue
            if candidate not in seen and not input_already_run(conn, candidate, target):
                seen.add(candidate)
                batch.append(candidate)
                break
            candidate = mutate_fn(
                seed_text,
                mutator_kind=mutator_kind,
                rng=rng,
            )
        else:
            seen.add(candidate)
            batch.append(candidate)
    return batch
