"""
Byte-havoc mutator: repeatedly applies AFL-style byte-level mutations to the
UTF-8 representation of the input.
"""
from __future__ import annotations

import random
from collections.abc import Callable

from .lib import (
    arithmetic_mutation,
    bit_flip,
    clone_block_mutation,
    delete_block_mutation,
    generate_ip_input,
    generate_json_input,
    interesting_value_mutation,
)

ByteMutationFn = Callable[..., bytes]


def _generate_seed_text(*, mutator_kind: str, rng: random.Random) -> str:
    if mutator_kind == "ip":
        return generate_ip_input(rng=rng)
    return generate_json_input(rng=rng)


def _run_byte_havoc(*, data: bytes, rng: random.Random) -> bytes:
    strategies: tuple[ByteMutationFn, ...] = (
        bit_flip,
        arithmetic_mutation,
        interesting_value_mutation,
        delete_block_mutation,
        clone_block_mutation,
    )
    mutated = data
    mutation_count = 1 << rng.randint(1, 4)

    for _ in range(mutation_count):
        strategy = rng.choice(strategies)
        mutated = strategy(data=mutated, rng=rng)

    return mutated


def mutate(
    text: str,
    *,
    mutator_kind: str,
    rng: random.Random,
) -> str:
    """
    Mutate text by applying several rounds of byte-level havoc to its UTF-8
    encoding, then decoding the result back into a string.
    """
    base_text = text if text else _generate_seed_text(mutator_kind=mutator_kind, rng=rng)
    base_bytes = base_text.encode("utf-8")
    mutated_bytes = _run_byte_havoc(data=base_bytes, rng=rng)

    if not mutated_bytes:
        return _generate_seed_text(mutator_kind=mutator_kind, rng=rng)

    mutated_text = mutated_bytes.decode("utf-8", errors="replace")
    if mutated_text:
        return mutated_text
    return _generate_seed_text(mutator_kind=mutator_kind, rng=rng)
