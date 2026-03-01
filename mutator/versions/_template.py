"""
TEMPLATE: Copy to a new file (e.g. random_bytes.py), implement mutate,
then in versions/__init__.py add:
    from . import random_bytes
    REGISTRY["random_bytes"] = random_bytes.mutate
"""
from __future__ import annotations

import random


def mutate(
    text: str,
    *,
    mutator_kind: str,
    rng: random.Random,
) -> str:
    """
    Return a mutated version of text.

    mutator_kind: "json", "ip", or other (chosen by main from target / --mutator).
    rng: use for all randomness.
    """
    # TODO: implement your mutation strategy
    _ = mutator_kind
    return text  # or mutated string
