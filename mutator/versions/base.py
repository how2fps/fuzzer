"""
Base mutator: grammar-based JSON and IP mutation with auto-dispatch by kind.
"""
from __future__ import annotations

import random

from ..mutator import mutate_json, mutate_ip, JSON_GRAMMAR, IP_GRAMMAR


def mutate(
    text: str,
    *,
    mutator_kind: str,
    rng: random.Random,
) -> str:
    """Mutate seed text; mutator_kind is 'json', 'ip', or inferred from target."""
    if mutator_kind == "ip":
        return mutate_ip(original_text=text, rng=rng)
    return mutate_json(original_text=text, rng=rng)
