import random
import re
import json
from typing import TypeAlias, Any
from .operators import AdaptiveStrategy, JSON_OPERATORS, IP_OPERATORS

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
        "<ipv4_input>": [
            "<ipv4>",
            "<ipv4>/<prefix4>",
            "<ipv4>/<prefix4_invalid>",  # invalid prefix
        ],
        "<ipv4>": [
            "<octet>.<octet>.<octet>.<octet>",
            "<octet_lz>.<octet_lz>.<octet_lz>.<octet_lz>",  # leading zeros
            "<octet>.<octet>.<octet>",                       # too few parts
            "<octet>.<octet>.<octet>.<octet>.<octet>",       # too many parts
        ],
        "<octet>": [
            # valid
            "0", "1", "10", "127", "128", "192", "223", "254", "255",
            # out-of-range
            "256", "300", "999", "-1",
            # broadcast / loopback / special
            "0", "255",
        ],
        "<octet_lz>": [
            "00", "01", "001", "010", "0127", "0192", "0255",
        ],
        "<prefix4>": ["0", "1", "8", "16", "24", "30", "31", "32"],
        "<prefix4_invalid>": ["-1", "33", "64", "128", "255", "abc"],
    },
}

IPV6_GRAMMAR: GrammarSpec = {
    "start": "<ipv6_input>",
    "recursive_symbols": set(),
    "rules": {
        "<ipv6_input>": [
            "<ipv6>",
            "<ipv6>/<prefix6>",
            "<ipv6>/<prefix6_invalid>",
            "<ipv6><zone_id>",             # zone ID (link-local)
            "<ipv6><zone_id>/<prefix6>",
        ],
        "<ipv6>": [
            # full form
            "<h>:<h>:<h>:<h>:<h>:<h>:<h>:<h>",
            # compressed forms
            "<h>::<h>",
            "<h>::<h>:<h>",
            "::",
            "::1",
            "::0",
            # common prefixes
            "fe80::<h>",
            "fe80::<h>:<h>",
            "2001:db8::<h>:<h>",
            "2001:db8::",
            # multicast
            "ff02::1",
            "ff02::2",
            "ff00::",
            # all zeros / all ones
            "0:0:0:0:0:0:0:0",
            "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
            # IPv4-mapped
            "::ffff:<ipv4_mapped>",
            "::ffff:0:<ipv4_mapped>",
            "64:ff9b::<ipv4_mapped>",  # RFC6052 well-known prefix
            # malformed
            "<h>:<h>:<h>:<h>:<h>:<h>:<h>:<h>:<h>",  # too many groups
            "<h>:<h>",                                # too few groups
            ":::",
        ],
        "<h>": [
            "0", "1", "a", "f", "10", "ff", "100", "fff",
            "0abc", "ffff", "FFFF", "AbCd",
            # out-of-range
            "10000", "fffff",
        ],
        "<ipv4_mapped>": [
            "192.168.0.1", "127.0.0.1", "0.0.0.0",
            "255.255.255.255", "10.0.0.1",
        ],
        "<zone_id>": [
            "%eth0", "%lo", "%en0", "%25eth0", "%", "%0",
        ],
        "<prefix6>": ["0", "32", "48", "64", "96", "128"],
        "<prefix6_invalid>": ["-1", "129", "256", "abc"],
    },
}

IP_GRAMMAR: GrammarSpec = {
    "start": "<ip>",
    "recursive_symbols": set(),
    "rules": {
        "<ip>": [
            "<ipv4_input>",
            "<ipv6_input>",
            # bare junk
            "",
            "not-an-ip",
            "localhost",
            "0x7f000001",   # hex IPv4
            "2130706433",   # decimal IPv4
        ],
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

_MAX_PENDING = 10_000


class GrammarFuzzer:
    def __init__(self):
        self.strategies = {
            "json": AdaptiveStrategy(list(JSON_OPERATORS.keys())),
            "ip": AdaptiveStrategy(list(IP_OPERATORS.keys())),
        }
        # Maps mutated_text -> (grammar_type, op_name) for deferred feedback.
        # Populated when an operator is chosen; cleared when coverage signal arrives.
        self._pending: dict[str, tuple[str, str]] = {}

    def mutate(
        self,
        text: str,
        grammar_type: str,
        grammar_spec: GrammarSpec,
        max_depth: int = 5,
        rng: random.Random | None = None,
    ) -> str:
        rng = rng or random.Random()
        strategy = self.strategies.get(grammar_type)

        # Decide whether to use a semantic operator, standard grammar mutation, or byte-level
        choice = rng.random()

        if choice < 0.4 and strategy:
            # 40% chance: Use a semantic operator   
            # 40% — "smart" mutations
            # Knows what the data means
            # JSON: parse it, run a semantic operator (mutate_keys, numeric_edge_case, etc.)
            # IP:   run an IP-aware operator (leading_zeros, zone_id, etc.)
            # Adaptive: learns which operators find more bugs over time
            op_name = strategy.select_operator(rng)
            operators = JSON_OPERATORS if grammar_type == "json" else IP_OPERATORS
            op_func = operators[op_name]

            if grammar_type == "json":
                try:
                    data = json.loads(text) if text else {}
                    mutated_data = op_func(data, rng)
                    # Handle cases where op_func returns a string (like duplicate_keys)
                    if isinstance(mutated_data, str):
                        result = mutated_data
                    else:
                        result = json.dumps(mutated_data)
                except json.JSONDecodeError:
                    result = mutate_text_with_grammar(original_text=text, grammar_spec=grammar_spec, rng=rng)
            else:
                result = op_func(text, rng)

            # Record which operator produced this output so the fuzzer loop can
            # call record_coverage() with the real new_coverage signal later.
            if len(self._pending) >= _MAX_PENDING:
                # Evict the oldest entry to keep memory bounded.
                self._pending.pop(next(iter(self._pending)))
            self._pending[result] = (grammar_type, op_name)
            return result

        elif choice < 0.7:
            # 30% — "structural" mutations
            # Knows it's text with grammar rules
            # Cuts the input apart and splices in grammar-generated fragments
            # Still produces mostly valid-looking inputs
            return mutate_text_with_grammar(
                original_text=text,
                grammar_spec=grammar_spec,
                max_depth=max_depth,
                rng=rng
            )
        else:
            # 30% — "dumb" mutations
            # Treats input as raw bytes, doesn't care what it means
            # Flips bits, increments bytes, deletes/clones chunks
            # Finds low-level memory/encoding bugs
            data = text.encode('utf-8', errors='replace')
            mutators = [bit_flip, arithmetic_mutation, interesting_value_mutation, delete_block_mutation, clone_block_mutation]
            func = rng.choice(mutators)
            return func(data=data, rng=rng).decode('utf-8', errors='ignore')

    def record_coverage(self, mutated_text: str, gained_coverage: bool) -> None:
        """Update the operator weight based on real coverage feedback.

        Call this after the interestingness check for a mutated input.
        If the text was not produced by a semantic operator (e.g. it came from
        grammar or byte-havoc), this is a no-op.
        """
        entry = self._pending.pop(mutated_text, None)
        if entry is not None:
            grammar_type, op_name = entry
            strategy = self.strategies.get(grammar_type)
            if strategy is not None:
                strategy.update_score(op_name, gained_coverage)


# Global fuzzer instance
_GLOBAL_FUZZER = GrammarFuzzer()


def mutate_json(original_text: str = "", rng: random.Random = None) -> str:
    return _GLOBAL_FUZZER.mutate(
        text=original_text,
        grammar_type="json",
        grammar_spec=JSON_GRAMMAR,
        rng=rng
    )


def mutate_ip(original_text: str = "", rng: random.Random = None) -> str:
    return _GLOBAL_FUZZER.mutate(
        text=original_text,
        grammar_type="ip",
        grammar_spec=IP_GRAMMAR,
        rng=rng
    )


def record_operator_coverage(mutated_text: str, gained_coverage: bool) -> None:
    """Call this after the interestingness check to give the adaptive strategy real feedback.

    If mutated_text was produced by a semantic operator, the operator's weight
    is updated based on whether it discovered new coverage.
    Non-operator mutations (grammar / byte-havoc) are silently ignored.
    """
    _GLOBAL_FUZZER.record_coverage(mutated_text, gained_coverage)