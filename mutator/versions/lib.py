from __future__ import annotations

import ipaddress
import json
import random
import re
from pathlib import Path
from typing import TypeAlias

GrammarRules: TypeAlias = dict[str, list[str]]
GrammarSpec: TypeAlias = dict[str, object]

VALID_OUTPUT_PROBABILITY = 0.7

_NON_TERMINAL_PATTERN = re.compile(r"<[^<>]+>")
_INTERESTING_BYTE_VALUES = (0x00, 0x01, 0x0A, 0x0D, 0x20, 0x7F, 0x80, 0xFE, 0xFF)
_GRAMMARS_DIR = Path(__file__).resolve().parent.parent / "grammars"

def _normalize_recursive_symbols(
    *, value: object, source: str
) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, set):
        symbols = value
    elif isinstance(value, (list, tuple)):
        symbols = value
    else:
        raise TypeError(
            f"{source}: grammar_spec['recursive_symbols'] must be a set or list"
        )

    normalized_symbols: set[str] = set()
    for symbol in symbols:
        if not isinstance(symbol, str):
            raise TypeError(
                f"{source}: grammar_spec['recursive_symbols'] entries must be strings"
            )
        normalized_symbols.add(symbol)
    return normalized_symbols


def _normalize_rules(*, value: object, source: str) -> GrammarRules:
    if not isinstance(value, dict):
        raise TypeError(f"{source}: grammar_spec['rules'] must be a dictionary")

    normalized_rules: GrammarRules = {}
    for symbol, productions in value.items():
        if not isinstance(symbol, str):
            raise TypeError(f"{source}: grammar rule names must be strings")
        if not isinstance(productions, list) or not productions:
            raise TypeError(
                f"{source}: grammar_spec['rules'][{symbol!r}] must be a non-empty list"
            )
        if not all(isinstance(option, str) for option in productions):
            raise TypeError(
                f"{source}: grammar_spec['rules'][{symbol!r}] entries must be strings"
            )
        normalized_rules[symbol] = list(productions)
    return normalized_rules


def normalize_grammar_spec(
    *, grammar_spec: GrammarSpec | dict[str, object], source: str = "<memory>"
) -> GrammarSpec:
    if not isinstance(grammar_spec, dict):
        raise TypeError(f"{source}: grammar_spec must be a dictionary")

    start = grammar_spec.get("start")
    if not isinstance(start, str):
        raise TypeError(f"{source}: grammar_spec['start'] must be a string")

    rules = _normalize_rules(value=grammar_spec.get("rules"), source=source)
    recursive_symbols = _normalize_recursive_symbols(
        value=grammar_spec.get("recursive_symbols", []),
        source=source,
    )
    return {
        "start": start,
        "rules": rules,
        "recursive_symbols": recursive_symbols,
    }


def load_grammar_from_json(*, path: str | Path) -> GrammarSpec:
    grammar_path = Path(path)
    with grammar_path.open(encoding="utf-8") as file:
        raw_spec = json.load(file)
    return normalize_grammar_spec(grammar_spec=raw_spec, source=str(grammar_path))


JSON_GRAMMAR_PATH = _GRAMMARS_DIR / "json.json"
IP_GRAMMAR_PATH = _GRAMMARS_DIR / "ip.json"
IPV4_GRAMMAR_PATH = _GRAMMARS_DIR / "ipv4.json"
IPV6_GRAMMAR_PATH = _GRAMMARS_DIR / "ipv6.json"

JSON_GRAMMAR = load_grammar_from_json(path=JSON_GRAMMAR_PATH)
IP_GRAMMAR = load_grammar_from_json(path=IP_GRAMMAR_PATH)
IPV4_GRAMMAR = load_grammar_from_json(path=IPV4_GRAMMAR_PATH)
IPV6_GRAMMAR = load_grammar_from_json(path=IPV6_GRAMMAR_PATH)

_DEFAULT_GRAMMARS: dict[str, GrammarSpec] = {
    "json": JSON_GRAMMAR,
    "ip": IP_GRAMMAR,
    "ipv4": IPV4_GRAMMAR,
    "ipv6": IPV6_GRAMMAR,
}
_ACTIVE_GRAMMARS: dict[str, GrammarSpec] = dict(_DEFAULT_GRAMMARS)


def configure_runtime_grammar(
    *, kind: str, grammar_path: str | Path | None = None
) -> None:
    if kind not in _ACTIVE_GRAMMARS:
        raise ValueError(
            f"Unsupported grammar kind {kind!r}. Must be one of: {sorted(_ACTIVE_GRAMMARS)}"
        )
    if grammar_path is None:
        _ACTIVE_GRAMMARS[kind] = _DEFAULT_GRAMMARS[kind]
        return
    _ACTIVE_GRAMMARS[kind] = load_grammar_from_json(path=grammar_path)


def _resolve_grammar_spec(*, kind: str, grammar_path: str | Path | None = None) -> GrammarSpec:
    if grammar_path is not None:
        return load_grammar_from_json(path=grammar_path)
    return _ACTIVE_GRAMMARS[kind]


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

    normalized_spec = normalize_grammar_spec(grammar_spec=grammar_spec)
    random_engine = rng or random.Random()
    start = normalized_spec["start"]
    rules = normalized_spec["rules"]
    recursive_symbols = normalized_spec["recursive_symbols"]

    return _expand_symbol(
        symbol=start,
        rules=rules,
        recursive_symbols=recursive_symbols,
        depth=0,
        max_depth=max_depth,
        rng=random_engine,
    )


def generate_json_input(
    *,
    max_depth: int = 6,
    rng: random.Random | None = None,
    grammar_path: str | Path | None = None,
) -> str:
    return generate_from_grammar(
        grammar_spec=_resolve_grammar_spec(kind="json", grammar_path=grammar_path),
        max_depth=max_depth,
        rng=rng,
    )


def generate_ip_input(
    *,
    max_depth: int = 3,
    rng: random.Random | None = None,
    grammar_path: str | Path | None = None,
) -> str:
    return generate_from_grammar(
        grammar_spec=_resolve_grammar_spec(kind="ip", grammar_path=grammar_path),
        max_depth=max_depth,
        rng=rng,
    )


def generate_ipv4_input(
    *,
    max_depth: int = 2,
    rng: random.Random | None = None,
    grammar_path: str | Path | None = None,
) -> str:
    return generate_from_grammar(
        grammar_spec=_resolve_grammar_spec(kind="ipv4", grammar_path=grammar_path),
        max_depth=max_depth,
        rng=rng,
    )


def generate_ipv6_input(
    *,
    max_depth: int = 2,
    rng: random.Random | None = None,
    grammar_path: str | Path | None = None,
) -> str:
    return generate_from_grammar(
        grammar_spec=_resolve_grammar_spec(kind="ipv6", grammar_path=grammar_path),
        max_depth=max_depth,
        rng=rng,
    )


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


def _validate_probability(*, name: str, value: float) -> float:
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be between 0.0 and 1.0")
    return value


def _mutate_text_from_original(
    *,
    original_text: str,
    fragment: str,
    rng: random.Random,
) -> str:
    if not original_text:
        return fragment

    strategy = rng.choice(("insert", "replace", "delete"))
    start = rng.randrange(len(original_text))
    end = rng.randrange(start + 1, len(original_text) + 1)

    if strategy == "insert":
        return original_text[:start] + fragment + original_text[start:]
    if strategy == "replace":
        return original_text[:start] + fragment + original_text[end:]
    if len(original_text) == 1:
        return original_text + fragment
    return original_text[:start] + original_text[end:]


def _generate_json_value(
    *,
    grammar_spec: GrammarSpec,
    max_depth: int,
    rng: random.Random,
) -> object:
    return json.loads(
        generate_from_grammar(
            grammar_spec=grammar_spec,
            max_depth=max(1, max_depth),
            rng=rng,
        )
    )


def _mutate_json_scalar(
    *,
    value: object,
    grammar_spec: GrammarSpec,
    max_depth: int,
    rng: random.Random,
) -> object:
    if isinstance(value, bool):
        return not value
    if value is None:
        return _generate_json_value(grammar_spec=grammar_spec, max_depth=max_depth, rng=rng)
    if isinstance(value, int):
        return value + rng.choice((-17, -3, -1, 1, 3, 17))
    if isinstance(value, float):
        return round(value + rng.choice((-3.5, -1.0, 1.0, 3.5)), 3)
    if isinstance(value, str):
        if not value:
            return "mutated"
        action = rng.choice(("insert", "replace", "delete"))
        index = rng.randrange(len(value))
        token = rng.choice(("a", "b", "_", "json", "0"))
        if action == "insert":
            return value[:index] + token + value[index:]
        if action == "replace":
            return value[:index] + token + value[index + 1 :]
        if len(value) == 1:
            return value + token
        return value[:index] + value[index + 1 :]
    return _generate_json_value(grammar_spec=grammar_spec, max_depth=max_depth, rng=rng)


def _change_json_container_type(
    *,
    value: object,
    max_depth: int,
    rng: random.Random,
) -> object:
    del max_depth
    if isinstance(value, dict):
        entries = [{"key": key, "value": item} for key, item in value.items()]
        if rng.random() < 0.3:
            return [{"entries": entries}]
        return entries
    if isinstance(value, list):
        if rng.random() < 0.5:
            return {"items": list(value)}
        return {f"item_{index}": item for index, item in enumerate(value)}
    if rng.random() < 0.5:
        return [value]
    return {"value": value}


def _wrap_json_value_recursively(
    *,
    value: object,
    max_depth: int,
    rng: random.Random,
) -> object:
    wrapped = value
    layer_count = rng.randint(1, max(1, min(3, max_depth + 1)))
    for layer_index in range(layer_count):
        if rng.random() < 0.5:
            wrapped = [wrapped]
            continue
        wrapped = {f"layer_{layer_index + 1}": wrapped}
    return wrapped


def _promote_json_child(*, value: object, rng: random.Random) -> object:
    if isinstance(value, dict) and value:
        return rng.choice(list(value.values()))
    if isinstance(value, list) and value:
        return rng.choice(value)
    return value


def _recurse_json_value(
    *,
    value: object,
    grammar_spec: GrammarSpec,
    max_depth: int,
    rng: random.Random,
) -> object:
    if max_depth < 1:
        return _mutate_json_scalar(
            value=value,
            grammar_spec=grammar_spec,
            max_depth=1,
            rng=rng,
        )

    if isinstance(value, dict):
        mutated = dict(value)
        if mutated and rng.random() < 0.8:
            target_key = rng.choice(list(mutated))
            if len(mutated) > 1 and rng.random() < 0.2:
                del mutated[target_key]
                return mutated
            mutated[target_key] = _mutate_json_value(
                value=mutated[target_key],
                grammar_spec=grammar_spec,
                max_depth=max_depth - 1,
                rng=rng,
            )
            return mutated

        mutated[f"mut_{rng.randrange(1000)}"] = _generate_json_value(
            grammar_spec=grammar_spec,
            max_depth=max_depth - 1,
            rng=rng,
        )
        return mutated

    if isinstance(value, list):
        mutated = list(value)
        if mutated and rng.random() < 0.8:
            target_index = rng.randrange(len(mutated))
            if len(mutated) > 1 and rng.random() < 0.2:
                mutated.pop(target_index)
                return mutated
            mutated[target_index] = _mutate_json_value(
                value=mutated[target_index],
                grammar_spec=grammar_spec,
                max_depth=max_depth - 1,
                rng=rng,
            )
            return mutated

        insert_at = rng.randrange(len(mutated) + 1)
        mutated.insert(
            insert_at,
            _generate_json_value(
                grammar_spec=grammar_spec,
                max_depth=max_depth - 1,
                rng=rng,
            ),
        )
        return mutated

    if rng.random() < 0.35:
        return _generate_json_value(grammar_spec=grammar_spec, max_depth=max_depth, rng=rng)
    return _mutate_json_scalar(
        value=value,
        grammar_spec=grammar_spec,
        max_depth=max_depth,
        rng=rng,
    )


def _mutate_json_value(
    *,
    value: object,
    grammar_spec: GrammarSpec,
    max_depth: int,
    rng: random.Random,
) -> object:
    if max_depth < 1:
        return _recurse_json_value(
            value=value,
            grammar_spec=grammar_spec,
            max_depth=0,
            rng=rng,
        )

    mutation_strategy = rng.choices(
        population=(
            "recurse",
            "replace_subtree",
            "wrap",
            "change_container_type",
            "promote_child",
        ),
        weights=(45, 22, 17, 12, 4),
        k=1,
    )[0]

    if mutation_strategy == "replace_subtree":
        return _generate_json_value(grammar_spec=grammar_spec, max_depth=max_depth, rng=rng)
    if mutation_strategy == "wrap":
        return _wrap_json_value_recursively(value=value, max_depth=max_depth, rng=rng)
    if mutation_strategy == "change_container_type":
        return _change_json_container_type(value=value, max_depth=max_depth, rng=rng)
    if mutation_strategy == "promote_child":
        promoted = _promote_json_child(value=value, rng=rng)
        if promoted is not value:
            return promoted

    return _recurse_json_value(
        value=value,
        grammar_spec=grammar_spec,
        max_depth=max_depth,
        rng=rng,
    )


def _mutate_valid_json_text(
    *,
    original_text: str,
    grammar_spec: GrammarSpec,
    max_depth: int,
    rng: random.Random,
) -> str:
    if not original_text:
        return generate_from_grammar(grammar_spec=grammar_spec, max_depth=max_depth, rng=rng)

    try:
        parsed = json.loads(original_text)
    except json.JSONDecodeError:
        fragment = generate_from_grammar(grammar_spec=grammar_spec, max_depth=max_depth, rng=rng)
        return _mutate_text_from_original(original_text=original_text, fragment=fragment, rng=rng)

    for _ in range(8):
        mutated_value = _mutate_json_value(
            value=parsed,
            grammar_spec=grammar_spec,
            max_depth=max_depth,
            rng=rng,
        )
        candidate = json.dumps(mutated_value, separators=(",", ":"))
        if candidate != original_text:
            return candidate

    return json.dumps(parsed, separators=(",", ":"))


def _mutate_invalid_json_text(
    *,
    original_text: str,
    grammar_spec: GrammarSpec,
    max_depth: int,
    rng: random.Random,
) -> str:
    if not original_text:
        valid_text = generate_from_grammar(grammar_spec=grammar_spec, max_depth=max_depth, rng=rng)
        return valid_text[:-1] if len(valid_text) > 1 else "{"

    structural_indexes = [idx for idx, char in enumerate(original_text) if char in '{}[],:\"']

    for _ in range(12):
        strategy = rng.choice(("remove_structural", "append_dangling", "truncate", "duplicate_separator"))
        candidate = original_text

        if strategy == "remove_structural" and structural_indexes:
            index = rng.choice(structural_indexes)
            candidate = original_text[:index] + original_text[index + 1 :]
        elif strategy == "append_dangling":
            candidate = original_text + rng.choice((",", ":", '"', "]", "}"))
        elif strategy == "truncate" and len(original_text) > 1:
            candidate = original_text[: rng.randrange(1, len(original_text))]
        elif strategy == "duplicate_separator":
            separator_indexes = [idx for idx, char in enumerate(original_text) if char in ",:"]
            if separator_indexes:
                index = rng.choice(separator_indexes)
                candidate = original_text[: index + 1] + original_text[index] + original_text[index + 1 :]

        if candidate == original_text:
            continue

        try:
            json.loads(candidate)
        except json.JSONDecodeError:
            return candidate

    return original_text + '"'


def _mutate_prefix_length(
    *,
    prefix_length: int,
    max_prefix_length: int,
    rng: random.Random,
) -> int:
    candidate_prefixes = {
        0,
        max_prefix_length,
        max(
            0,
            min(
                max_prefix_length,
                prefix_length + rng.choice((-16, -8, -4, -1, 1, 4, 8, 16)),
            ),
        ),
        rng.randint(0, max_prefix_length),
        min(max_prefix_length, max(0, prefix_length)),
    }
    return rng.choice(sorted(candidate_prefixes))


def _format_ip_address_text(
    *,
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    rng: random.Random,
) -> str:
    if isinstance(address, ipaddress.IPv6Address):
        if rng.random() < 0.4:
            return address.exploded
        return str(address)
    return str(address)


def _format_ip_interface_text(
    *,
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    prefix_length: int,
    rng: random.Random,
) -> str:
    return f"{_format_ip_address_text(address=address, rng=rng)}/{prefix_length}"


def _apply_host_mask(
    *,
    address_int: int,
    prefix_length: int,
    max_prefix_length: int,
    host_int: int,
) -> int:
    if prefix_length <= 0:
        return host_int & ((1 << max_prefix_length) - 1)
    if prefix_length >= max_prefix_length:
        return address_int

    host_bits = max_prefix_length - prefix_length
    host_mask = (1 << host_bits) - 1
    network_mask = ((1 << max_prefix_length) - 1) ^ host_mask
    network_part = address_int & network_mask
    return network_part | (host_int & host_mask)


def _randomize_host_bits(
    *,
    address_int: int,
    prefix_length: int,
    max_prefix_length: int,
    rng: random.Random,
) -> int:
    if prefix_length >= max_prefix_length:
        return address_int

    host_bits = max_prefix_length - prefix_length
    return _apply_host_mask(
        address_int=address_int,
        prefix_length=prefix_length,
        max_prefix_length=max_prefix_length,
        host_int=rng.randrange(1 << host_bits),
    )


def _mutate_ipv4_address_structural(
    *,
    address: ipaddress.IPv4Address,
    prefix_length: int | None,
    rng: random.Random,
) -> ipaddress.IPv4Address:
    effective_prefix = 24 if prefix_length is None else prefix_length
    octets = [int(part) for part in str(address).split(".")]
    mutation_strategy = rng.choice(
        (
            "single_octet",
            "multiple_octets",
            "swap_octets",
            "zero_suffix",
            "randomize_suffix",
            "preserve_prefix_change_host",
        )
    )

    if mutation_strategy == "single_octet":
        target_index = rng.randrange(4)
        octets[target_index] = rng.randrange(256)
        return ipaddress.IPv4Address(".".join(str(part) for part in octets))

    if mutation_strategy == "multiple_octets":
        for target_index in rng.sample(range(4), k=rng.randint(2, 4)):
            octets[target_index] = rng.randrange(256)
        return ipaddress.IPv4Address(".".join(str(part) for part in octets))

    if mutation_strategy == "swap_octets":
        left_index, right_index = rng.sample(range(4), k=2)
        octets[left_index], octets[right_index] = octets[right_index], octets[left_index]
        return ipaddress.IPv4Address(".".join(str(part) for part in octets))

    address_int = int(address)
    if mutation_strategy == "zero_suffix":
        return ipaddress.IPv4Address(
            _apply_host_mask(
                address_int=address_int,
                prefix_length=effective_prefix,
                max_prefix_length=32,
                host_int=0,
            )
        )

    if mutation_strategy in {"randomize_suffix", "preserve_prefix_change_host"}:
        return ipaddress.IPv4Address(
            _randomize_host_bits(
                address_int=address_int,
                prefix_length=effective_prefix,
                max_prefix_length=32,
                rng=rng,
            )
        )

    return ipaddress.IPv4Address(".".join(str(part) for part in octets))


def _mutate_ipv4_address(
    *,
    address: ipaddress.IPv4Address,
    rng: random.Random,
    prefix_length: int | None = None,
) -> ipaddress.IPv4Address:
    if rng.random() < 0.2:
        octets = [int(part) for part in str(address).split(".")]
        target_index = rng.randrange(4)
        octets[target_index] = (octets[target_index] + rng.choice((-127, -31, -1, 1, 31, 127))) % 256
        return ipaddress.IPv4Address(".".join(str(part) for part in octets))
    return _mutate_ipv4_address_structural(
        address=address,
        prefix_length=prefix_length,
        rng=rng,
    )


def _mutate_ipv6_address_structural(
    *,
    address: ipaddress.IPv6Address,
    prefix_length: int | None,
    rng: random.Random,
) -> ipaddress.IPv6Address:
    effective_prefix = 64 if prefix_length is None else prefix_length
    hextets = [int(part, 16) for part in address.exploded.split(":")]
    mutation_strategy = rng.choice(
        (
            "single_hextet",
            "multiple_hextets",
            "swap_hextets",
            "zero_suffix",
            "randomize_suffix",
            "preserve_prefix_change_host",
        )
    )

    if mutation_strategy == "single_hextet":
        target_index = rng.randrange(8)
        hextets[target_index] = rng.randrange(65536)
        return ipaddress.IPv6Address(":".join(f"{part:x}" for part in hextets))

    if mutation_strategy == "multiple_hextets":
        for target_index in rng.sample(range(8), k=rng.randint(2, 4)):
            hextets[target_index] = rng.randrange(65536)
        return ipaddress.IPv6Address(":".join(f"{part:x}" for part in hextets))

    if mutation_strategy == "swap_hextets":
        left_index, right_index = rng.sample(range(8), k=2)
        hextets[left_index], hextets[right_index] = hextets[right_index], hextets[left_index]
        return ipaddress.IPv6Address(":".join(f"{part:x}" for part in hextets))

    address_int = int(address)
    if mutation_strategy == "zero_suffix":
        return ipaddress.IPv6Address(
            _apply_host_mask(
                address_int=address_int,
                prefix_length=effective_prefix,
                max_prefix_length=128,
                host_int=0,
            )
        )

    if mutation_strategy in {"randomize_suffix", "preserve_prefix_change_host"}:
        return ipaddress.IPv6Address(
            _randomize_host_bits(
                address_int=address_int,
                prefix_length=effective_prefix,
                max_prefix_length=128,
                rng=rng,
            )
        )

    return ipaddress.IPv6Address(":".join(f"{part:x}" for part in hextets))


def _mutate_ipv6_address(
    *,
    address: ipaddress.IPv6Address,
    rng: random.Random,
    prefix_length: int | None = None,
) -> ipaddress.IPv6Address:
    if rng.random() < 0.2:
        hextets = address.exploded.split(":")
        target_index = rng.randrange(8)
        next_value = (int(hextets[target_index], 16) + rng.choice((-4096, -1, 1, 4096))) % 65536
        hextets[target_index] = f"{next_value:x}"
        return ipaddress.IPv6Address(":".join(hextets))
    return _mutate_ipv6_address_structural(
        address=address,
        prefix_length=prefix_length,
        rng=rng,
    )


def _mutate_ip_address(
    *,
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    rng: random.Random,
    prefix_length: int | None = None,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if isinstance(address, ipaddress.IPv4Address):
        return _mutate_ipv4_address(address=address, rng=rng, prefix_length=prefix_length)
    return _mutate_ipv6_address(address=address, rng=rng, prefix_length=prefix_length)


def _generate_ip_text(*, grammar_spec: GrammarSpec, max_depth: int, rng: random.Random) -> str:
    return generate_from_grammar(grammar_spec=grammar_spec, max_depth=max_depth, rng=rng)


def _mutate_ip_interface_text(
    *,
    interface: ipaddress.IPv4Interface | ipaddress.IPv6Interface,
    rng: random.Random,
) -> str:
    max_prefix_length = 32 if interface.version == 4 else 128
    mutation_strategy = rng.choice(
        (
            "mutate_address_only",
            "mutate_prefix_only",
            "preserve_network_change_host",
            "snap_to_network_boundary",
        )
    )

    if mutation_strategy == "mutate_address_only":
        mutated_address = _mutate_ip_address(
            address=interface.ip,
            prefix_length=interface.network.prefixlen,
            rng=rng,
        )
        return _format_ip_interface_text(
            address=mutated_address,
            prefix_length=interface.network.prefixlen,
            rng=rng,
        )

    if mutation_strategy == "mutate_prefix_only":
        mutated_prefix = _mutate_prefix_length(
            prefix_length=interface.network.prefixlen,
            max_prefix_length=max_prefix_length,
            rng=rng,
        )
        return _format_ip_interface_text(
            address=interface.ip,
            prefix_length=mutated_prefix,
            rng=rng,
        )

    if mutation_strategy == "snap_to_network_boundary":
        return _format_ip_interface_text(
            address=interface.network.network_address,
            prefix_length=interface.network.prefixlen,
            rng=rng,
        )

    mutated_address = type(interface.ip)(
        _randomize_host_bits(
            address_int=int(interface.ip),
            prefix_length=interface.network.prefixlen,
            max_prefix_length=max_prefix_length,
            rng=rng,
        )
    )
    return _format_ip_interface_text(
        address=mutated_address,
        prefix_length=interface.network.prefixlen,
        rng=rng,
    )


def _mutate_valid_ip_text(
    *,
    original_text: str,
    grammar_spec: GrammarSpec,
    max_depth: int,
    rng: random.Random,
) -> str:
    if not original_text:
        return _generate_ip_text(grammar_spec=grammar_spec, max_depth=max_depth, rng=rng)

    try:
        if "/" in original_text:
            interface = ipaddress.ip_interface(original_text)
            for _ in range(8):
                candidate = _mutate_ip_interface_text(interface=interface, rng=rng)
                if candidate != original_text:
                    return candidate
            return _format_ip_interface_text(
                address=interface.ip,
                prefix_length=interface.network.prefixlen,
                rng=rng,
            )

        address = ipaddress.ip_address(original_text)
        for _ in range(8):
            mutated_address = _mutate_ip_address(address=address, prefix_length=None, rng=rng)
            if rng.random() < 0.2:
                prefix_limit = 32 if mutated_address.version == 4 else 128
                default_prefix = 24 if mutated_address.version == 4 else 64
                candidate = _format_ip_interface_text(
                    address=mutated_address,
                    prefix_length=_mutate_prefix_length(
                        prefix_length=default_prefix,
                        max_prefix_length=prefix_limit,
                        rng=rng,
                    ),
                    rng=rng,
                )
            else:
                candidate = _format_ip_address_text(address=mutated_address, rng=rng)
            if candidate != original_text:
                return candidate
        return _format_ip_address_text(address=address, rng=rng)
    except ValueError:
        fragment = _generate_ip_text(grammar_spec=grammar_spec, max_depth=max_depth, rng=rng)
        return _mutate_text_from_original(original_text=original_text, fragment=fragment, rng=rng)


def _is_valid_ip_text(text: str) -> bool:
    try:
        if "/" in text:
            ipaddress.ip_interface(text)
        else:
            ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


def _mutate_invalid_ip_text(
    *,
    original_text: str,
    grammar_spec: GrammarSpec,
    max_depth: int,
    rng: random.Random,
) -> str:
    if not original_text:
        valid_text = _generate_ip_text(grammar_spec=grammar_spec, max_depth=max_depth, rng=rng)
        original_text = valid_text

    for _ in range(12):
        candidate = original_text
        if "." in original_text:
            strategy = rng.choice(("bad_octet", "bad_prefix", "double_dot"))
            if strategy == "bad_octet":
                candidate = re.sub(r"\d+", "999", original_text, count=1)
            elif strategy == "bad_prefix" and "/" in original_text:
                candidate = re.sub(r"/\d+$", "/33", original_text)
            else:
                candidate = original_text.replace(".", "..", 1)
        elif ":" in original_text:
            strategy = rng.choice(("bad_hextet", "bad_prefix", "duplicate_colon"))
            if strategy == "bad_hextet":
                candidate = re.sub(r"[0-9a-fA-F]+", "gggg", original_text, count=1)
            elif strategy == "bad_prefix" and "/" in original_text:
                candidate = re.sub(r"/\d+$", "/129", original_text)
            else:
                candidate = original_text.replace(":", ":::", 1)
        elif "/" in original_text:
            candidate = re.sub(r"/\d+$", "/999", original_text)
        elif len(original_text) > 1:
            cut_index = rng.randrange(len(original_text))
            candidate = original_text[:cut_index] + original_text[cut_index + 1 :]
        else:
            candidate = original_text + "/999"

        if candidate != original_text and not _is_valid_ip_text(candidate):
            return candidate

    return original_text + "/999"


def mutate_text_with_grammar(
    *,
    original_text: str,
    grammar_spec: GrammarSpec,
    max_depth: int = 5,
    regenerate_probability: float = 0.35,
    rng: random.Random | None = None,
) -> str:
    random_engine = rng or random.Random()
    if not original_text:
        return generate_from_grammar(grammar_spec=grammar_spec, max_depth=max_depth, rng=random_engine)

    fragment = generate_from_grammar(grammar_spec=grammar_spec, max_depth=max_depth, rng=random_engine)
    return _mutate_text_from_original(original_text=original_text, fragment=fragment, rng=random_engine)


def mutate_json_input(
    *,
    original_text: str = "",
    max_depth: int = 6,
    regenerate_probability: float = 0.35,
    rng: random.Random | None = None,
    grammar_path: str | Path | None = None,
) -> str:
    del regenerate_probability
    random_engine = rng or random.Random()
    grammar_spec = _resolve_grammar_spec(kind="json", grammar_path=grammar_path)
    valid_output_probability = _validate_probability(
        name="VALID_OUTPUT_PROBABILITY",
        value=VALID_OUTPUT_PROBABILITY,
    )
    if random_engine.random() < valid_output_probability:
        return _mutate_valid_json_text(
            original_text=original_text,
            grammar_spec=grammar_spec,
            max_depth=max_depth,
            rng=random_engine,
        )
    return _mutate_invalid_json_text(
        original_text=original_text,
        grammar_spec=grammar_spec,
        max_depth=max_depth,
        rng=random_engine,
    )


def mutate_ip_input(
    *,
    original_text: str = "",
    max_depth: int = 3,
    regenerate_probability: float = 0.35,
    rng: random.Random | None = None,
    grammar_path: str | Path | None = None,
) -> str:
    del regenerate_probability
    random_engine = rng or random.Random()
    grammar_spec = _resolve_grammar_spec(kind="ip", grammar_path=grammar_path)
    valid_output_probability = _validate_probability(
        name="VALID_OUTPUT_PROBABILITY",
        value=VALID_OUTPUT_PROBABILITY,
    )
    if random_engine.random() < valid_output_probability:
        return _mutate_valid_ip_text(
            original_text=original_text,
            grammar_spec=grammar_spec,
            max_depth=max_depth,
            rng=random_engine,
        )
    return _mutate_invalid_ip_text(
        original_text=original_text,
        grammar_spec=grammar_spec,
        max_depth=max_depth,
        rng=random_engine,
    )


def mutate_ipv4_input(
    *,
    original_text: str = "",
    max_depth: int = 2,
    regenerate_probability: float = 0.35,
    rng: random.Random | None = None,
    grammar_path: str | Path | None = None,
) -> str:
    del regenerate_probability
    random_engine = rng or random.Random()
    grammar_spec = _resolve_grammar_spec(kind="ipv4", grammar_path=grammar_path)
    valid_output_probability = _validate_probability(
        name="VALID_OUTPUT_PROBABILITY",
        value=VALID_OUTPUT_PROBABILITY,
    )
    if random_engine.random() < valid_output_probability:
        return _mutate_valid_ip_text(
            original_text=original_text,
            grammar_spec=grammar_spec,
            max_depth=max_depth,
            rng=random_engine,
        )
    return _mutate_invalid_ip_text(
        original_text=original_text,
        grammar_spec=grammar_spec,
        max_depth=max_depth,
        rng=random_engine,
    )


def mutate_ipv6_input(
    *,
    original_text: str = "",
    max_depth: int = 2,
    regenerate_probability: float = 0.35,
    rng: random.Random | None = None,
    grammar_path: str | Path | None = None,
) -> str:
    del regenerate_probability
    random_engine = rng or random.Random()
    grammar_spec = _resolve_grammar_spec(kind="ipv6", grammar_path=grammar_path)
    valid_output_probability = _validate_probability(
        name="VALID_OUTPUT_PROBABILITY",
        value=VALID_OUTPUT_PROBABILITY,
    )
    if random_engine.random() < valid_output_probability:
        return _mutate_valid_ip_text(
            original_text=original_text,
            grammar_spec=grammar_spec,
            max_depth=max_depth,
            rng=random_engine,
        )
    return _mutate_invalid_ip_text(
        original_text=original_text,
        grammar_spec=grammar_spec,
        max_depth=max_depth,
        rng=random_engine,
    )


__all__ = [
    "JSON_GRAMMAR",
    "JSON_GRAMMAR_PATH",
    "IP_GRAMMAR",
    "IP_GRAMMAR_PATH",
    "IPV4_GRAMMAR",
    "IPV4_GRAMMAR_PATH",
    "IPV6_GRAMMAR",
    "IPV6_GRAMMAR_PATH",
    "VALID_OUTPUT_PROBABILITY",
    "arithmetic_mutation",
    "bit_flip",
    "clone_block_mutation",
    "configure_runtime_grammar",
    "delete_block_mutation",
    "generate_from_grammar",
    "generate_ip_input",
    "generate_ipv4_input",
    "generate_ipv6_input",
    "generate_json_input",
    "interesting_value_mutation",
    "load_grammar_from_json",
    "mutate_ip_input",
    "mutate_ipv4_input",
    "mutate_ipv6_input",
    "mutate_json_input",
    "mutate_text_with_grammar",
    "normalize_grammar_spec",
]
