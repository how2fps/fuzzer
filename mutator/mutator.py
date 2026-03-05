from __future__ import annotations

import random
import re
from typing import TypeAlias

GrammarRules: TypeAlias = dict[str, list[str]]
GrammarSpec: TypeAlias = dict[str, object]

_NON_TERMINAL_PATTERN = re.compile(r"<[^<>]+>")
_INTERESTING_BYTE_VALUES = (0x00, 0x01, 0x0A, 0x0D, 0x20, 0x7F, 0x80, 0xFE, 0xFF)


JSON_GRAMMAR: GrammarSpec = {
    "start": "<json>",
    "recursive_symbols": {"<object>", "<array>", "<members>", "<elements>", "<value>"},
    "rules": {
        "<json>": ["<value>"],
        "<value>": ["<object>", "<array>", "<string>", "<number>", "true", "false", "null"],
        "<object>": ["{}", "{<members>}"],
        "<members>": ["<pair>", "<pair>,<members>"],
        "<pair>": ["<string>:<value>"],
        "<array>": ["[]", "[<elements>]"],
        "<elements>": ["<value>", "<value>,<elements>"],
        "<string>": ['"a"', '"b"', '"json"', '"ip"', '"\\u0030"', '"x y"', '"long_key_123"'],
        "<number>": ["0", "-1", "1", "42", "3.14", "-0.001", "1e10", "-2E-2"],
    },
}

IPV4_GRAMMAR: GrammarSpec = {
    "start": "<ipv4_input>",
    "recursive_symbols": set(),
    "rules": {
        "<ipv4_input>": ["<ipv4>", "<ipv4>/<prefix4>"],
        "<ipv4>": ["<octet>.<octet>.<octet>.<octet>"],
        "<octet>": ["0", "1", "10", "127", "192", "223", "254", "255"],
        "<prefix4>": ["0", "8", "16", "24", "30", "32"],
    },
}

IPV6_GRAMMAR: GrammarSpec = {
    "start": "<ipv6_input>",
    "recursive_symbols": set(),
    "rules": {
        "<ipv6_input>": ["<ipv6>", "<ipv6>/<prefix6>"],
        "<ipv6>": [
            "<h>:<h>:<h>:<h>:<h>:<h>:<h>:<h>",
            "<h>::<h>",
            "::1",
            "::",
            "fe80::<h>",
            "2001:db8::<h>:<h>",
        ],
        "<h>": ["0", "1", "a", "f", "10", "ff", "0abc", "ffff"],
        "<prefix6>": ["0", "32", "48", "64", "96", "128"],
    },
}

IP_GRAMMAR: GrammarSpec = {
    "start": "<ip>",
    "recursive_symbols": set(),
    "rules": {
        "<ip>": ["<ipv4_input>", "<ipv6_input>"],
        **IPV4_GRAMMAR["rules"],
        **IPV6_GRAMMAR["rules"],
    },
}


def _as_bytearray(data: bytes | bytearray) -> bytearray:
    return data if isinstance(data, bytearray) else bytearray(data)


def _pick_production(
    *,
    symbol: str,
    rules: GrammarRules,
    recursive_symbols: set[str],
    depth: int,
    max_depth: int,
    rng: random.Random,
) -> str:
    productions = rules[symbol]
    if depth < max_depth or symbol not in recursive_symbols:
        return rng.choice(productions)

    safe_productions = [
        option
        for option in productions
        if not any(token in recursive_symbols for token in _NON_TERMINAL_PATTERN.findall(option))
    ]
    return rng.choice(safe_productions or productions)


def _expand_symbol(
    *,
    symbol: str,
    rules: GrammarRules,
    recursive_symbols: set[str],
    depth: int,
    max_depth: int,
    rng: random.Random,
) -> str:
    if symbol not in rules:
        return symbol

    production = _pick_production(
        symbol=symbol,
        rules=rules,
        recursive_symbols=recursive_symbols,
        depth=depth,
        max_depth=max_depth,
        rng=rng,
    )
    parts: list[str] = []
    last_idx = 0

    for match in _NON_TERMINAL_PATTERN.finditer(production):
        parts.append(production[last_idx:match.start()])
        next_symbol = match.group(0)
        parts.append(
            _expand_symbol(
                symbol=next_symbol,
                rules=rules,
                recursive_symbols=recursive_symbols,
                depth=depth + 1,
                max_depth=max_depth,
                rng=rng,
            )
        )
        last_idx = match.end()

    parts.append(production[last_idx:])
    return "".join(parts)


def generate_from_grammar(
    *,
    grammar_spec: GrammarSpec,
    max_depth: int = 5,
    rng: random.Random | None = None,
) -> str:
    if max_depth < 1:
        raise ValueError("max_depth must be >= 1")

    random_engine = rng or random.Random()
    start = grammar_spec["start"]
    rules = grammar_spec["rules"]
    recursive_symbols = grammar_spec.get("recursive_symbols", set())

    if not isinstance(start, str):
        raise TypeError("grammar_spec['start'] must be a string")
    if not isinstance(rules, dict):
        raise TypeError("grammar_spec['rules'] must be a dictionary")
    if not isinstance(recursive_symbols, set):
        raise TypeError("grammar_spec['recursive_symbols'] must be a set")

    return _expand_symbol(
        symbol=start,
        rules=rules,
        recursive_symbols=recursive_symbols,
        depth=0,
        max_depth=max_depth,
        rng=random_engine,
    )


def generate_json_input(*, max_depth: int = 6, rng: random.Random | None = None) -> str:
    return generate_from_grammar(grammar_spec=JSON_GRAMMAR, max_depth=max_depth, rng=rng)


def generate_ip_input(*, max_depth: int = 3, rng: random.Random | None = None) -> str:
    return generate_from_grammar(grammar_spec=IP_GRAMMAR, max_depth=max_depth, rng=rng)


def generate_ipv4_input(*, max_depth: int = 2, rng: random.Random | None = None) -> str:
    return generate_from_grammar(grammar_spec=IPV4_GRAMMAR, max_depth=max_depth, rng=rng)


def generate_ipv6_input(*, max_depth: int = 2, rng: random.Random | None = None) -> str:
    return generate_from_grammar(grammar_spec=IPV6_GRAMMAR, max_depth=max_depth, rng=rng)


def bit_flip(*, data: bytes | bytearray, rng: random.Random | None = None) -> bytes:
    if not data:
        return b""

    random_engine = rng or random.Random()
    mutated = _as_bytearray(data)
    index = random_engine.randrange(len(mutated))
    bit = random_engine.randrange(8)
    mutated[index] ^= 1 << bit
    return bytes(mutated)


def arithmetic_mutation(*, data: bytes | bytearray, rng: random.Random | None = None) -> bytes:
    if not data:
        return b""

    random_engine = rng or random.Random()
    mutated = _as_bytearray(data)
    index = random_engine.randrange(len(mutated))
    delta = random_engine.choice((-35, -1, 1, 35))
    mutated[index] = (mutated[index] + delta) % 256
    return bytes(mutated)


def interesting_value_mutation(*, data: bytes | bytearray, rng: random.Random | None = None) -> bytes:
    random_engine = rng or random.Random()
    mutated = _as_bytearray(data)

    if not mutated:
        return bytes([random_engine.choice(_INTERESTING_BYTE_VALUES)])

    index = random_engine.randrange(len(mutated))
    mutated[index] = random_engine.choice(_INTERESTING_BYTE_VALUES)
    return bytes(mutated)


def delete_block_mutation(*, data: bytes | bytearray, rng: random.Random | None = None) -> bytes:
    if len(data) < 2:
        return bytes(data)

    random_engine = rng or random.Random()
    mutated = _as_bytearray(data)
    start = random_engine.randrange(len(mutated) - 1)
    max_len = len(mutated) - start
    block_len = random_engine.randint(1, max_len)
    del mutated[start : start + block_len]
    return bytes(mutated)


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
    return bytes(mutated)


def mutate_text_with_grammar(
    *,
    original_text: str,
    grammar_spec: GrammarSpec,
    max_depth: int = 5,
    regenerate_probability: float = 0.35,
    rng: random.Random | None = None,
) -> str:
    random_engine = rng or random.Random()
    if not original_text or random_engine.random() < regenerate_probability:
        return generate_from_grammar(grammar_spec=grammar_spec, max_depth=max_depth, rng=random_engine)

    strategy = random_engine.choice(("insert", "replace", "delete"))
    fragment = generate_from_grammar(grammar_spec=grammar_spec, max_depth=max_depth, rng=random_engine)
    start = random_engine.randrange(len(original_text))
    end = random_engine.randrange(start, len(original_text))

    if strategy == "insert":
        return original_text[:start] + fragment + original_text[start:]
    if strategy == "replace":
        return original_text[:start] + fragment + original_text[end:]
    if len(original_text) == 1:
        return ""
    return original_text[:start] + original_text[end:]


def mutate_json_input(
    *,
    original_text: str = "",
    max_depth: int = 6,
    regenerate_probability: float = 0.35,
    rng: random.Random | None = None,
) -> str:
    return mutate_text_with_grammar(
        original_text=original_text,
        grammar_spec=JSON_GRAMMAR,
        max_depth=max_depth,
        regenerate_probability=regenerate_probability,
        rng=rng,
    )


def mutate_ip_input(
    *,
    original_text: str = "",
    max_depth: int = 3,
    regenerate_probability: float = 0.35,
    rng: random.Random | None = None,
) -> str:
    return mutate_text_with_grammar(
        original_text=original_text,
        grammar_spec=IP_GRAMMAR,
        max_depth=max_depth,
        regenerate_probability=regenerate_probability,
        rng=rng,
    )


def mutate_ipv4_input(
    *,
    original_text: str = "",
    max_depth: int = 2,
    regenerate_probability: float = 0.35,
    rng: random.Random | None = None,
) -> str:
    return mutate_text_with_grammar(
        original_text=original_text,
        grammar_spec=IPV4_GRAMMAR,
        max_depth=max_depth,
        regenerate_probability=regenerate_probability,
        rng=rng,
    )


def mutate_ipv6_input(
    *,
    original_text: str = "",
    max_depth: int = 2,
    regenerate_probability: float = 0.35,
    rng: random.Random | None = None,
) -> str:
    return mutate_text_with_grammar(
        original_text=original_text,
        grammar_spec=IPV6_GRAMMAR,
        max_depth=max_depth,
        regenerate_probability=regenerate_probability,
        rng=rng,
    )
        