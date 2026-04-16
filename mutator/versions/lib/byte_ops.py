from __future__ import annotations

import random

from .shared import _INTERESTING_BYTE_VALUES, sanitize_mutated_bytes

def _as_bytearray(data: bytes | bytearray) -> bytearray:
    return data if isinstance(data, bytearray) else bytearray(data)

def bit_flip(*, data: bytes | bytearray, rng: random.Random | None = None) -> bytes:
    if not data:
        return b""

    random_engine = rng or random.Random()
    mutated = _as_bytearray(data)
    index = random_engine.randrange(len(mutated))
    bit = random_engine.randrange(8)
    mutated[index] ^= 1 << bit
    return sanitize_mutated_bytes(mutated)

def arithmetic_mutation(*, data: bytes | bytearray, rng: random.Random | None = None) -> bytes:
    if not data:
        return b""

    random_engine = rng or random.Random()
    mutated = _as_bytearray(data)
    index = random_engine.randrange(len(mutated))
    delta = random_engine.choice((-35, -1, 1, 35))
    mutated[index] = (mutated[index] + delta) % 256
    return sanitize_mutated_bytes(mutated)

def interesting_value_mutation(*, data: bytes | bytearray, rng: random.Random | None = None) -> bytes:
    random_engine = rng or random.Random()
    mutated = _as_bytearray(data)

    if not mutated:
        return sanitize_mutated_bytes(
            bytes([random_engine.choice(_INTERESTING_BYTE_VALUES)])
        )

    index = random_engine.randrange(len(mutated))
    mutated[index] = random_engine.choice(_INTERESTING_BYTE_VALUES)
    return sanitize_mutated_bytes(mutated)

def delete_block_mutation(*, data: bytes | bytearray, rng: random.Random | None = None) -> bytes:
    if len(data) < 2:
        return sanitize_mutated_bytes(data)

    random_engine = rng or random.Random()
    mutated = _as_bytearray(data)
    start = random_engine.randrange(len(mutated) - 1)
    max_len = len(mutated) - start
    block_len = random_engine.randint(1, max_len)
    del mutated[start : start + block_len]
    return sanitize_mutated_bytes(mutated)

def clone_block_mutation(*, data: bytes | bytearray, rng: random.Random | None = None) -> bytes:
    if not data:
        return b""

    random_engine = rng or random.Random()
    mutated = _as_bytearray(data)
    start = random_engine.randrange(len(mutated))
    max_len = len(mutated) - start
    block_len = random_engine.randint(1, max_len)
    block = mutated[start : start + block_len]
    insert_at = random_engine.randrange(len(mutated) + 1)
    mutated[insert_at:insert_at] = block
    return sanitize_mutated_bytes(mutated)

def extreme_repeat_mutation(*, data: bytes | bytearray, rng: random.Random | None = None) -> bytes:
    if not data:
        return b""
    random_engine = rng or random.Random()
    mutated = _as_bytearray(data)
    start = random_engine.randrange(len(mutated))
    max_len = min(len(mutated) - start, 4)
    block_len = random_engine.randint(1, max_len)
    block = mutated[start : start + block_len]
    repeat_count = random_engine.randint(500, 10000)
    insert_at = random_engine.randrange(len(mutated) + 1)
    mutated[insert_at:insert_at] = block * repeat_count
    return sanitize_mutated_bytes(mutated)

def insert_junk_mutation(*, data: bytes | bytearray, rng: random.Random | None = None) -> bytes:
    random_engine = rng or random.Random()
    mutated = _as_bytearray(data)
    if not mutated:
        insert_at = 0
    else:
        insert_at = random_engine.randrange(len(mutated) + 1)
    junk = bytes([random_engine.choice(b"\t\n\r\x17\x01\x02\x1f\x7f \x80\xff")])
    mutated[insert_at:insert_at] = junk
    return sanitize_mutated_bytes(mutated)

def insert_extreme_number_mutation(*, data: bytes | bytearray, rng: random.Random | None = None) -> bytes:
    random_engine = rng or random.Random()
    mutated = _as_bytearray(data)
    if not mutated:
        insert_at = 0
    else:
        insert_at = random_engine.randrange(len(mutated) + 1)
    numbers = [b"256", b"-1", b"-256", b"4294967296", b"4294967295", b"2147483648", b"-2147483649", b"999999999999999999999"]
    number = random_engine.choice(numbers)
    mutated[insert_at:insert_at] = number
    return sanitize_mutated_bytes(mutated)

__all__ = [
    "bit_flip",
    "arithmetic_mutation",
    "interesting_value_mutation",
    "delete_block_mutation",
    "clone_block_mutation",
    "extreme_repeat_mutation",
    "insert_junk_mutation",
    "insert_extreme_number_mutation",
]
