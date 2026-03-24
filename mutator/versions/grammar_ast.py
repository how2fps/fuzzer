"""
Generalized grammar-AST mutator inspired by mutator_test.py.

This version keeps the grammar DSL and generic node-type mutation operators:
Literal, CharClass, Sequence, Alternation, Repeat, NumberRange, and Ref.

The seed is used to:
- choose a sensible start rule (JSON object/value, IPv4/IPv6 with or without CIDR)
- bias which grammar rules are mutated more often
- optionally blend the generated candidate back with the original seed

Extra rules from ``-g/--grammar-rules-file`` are parsed into the same grammar.
If a rule name ends with ``_mut`` and its base rule exists, the base rule is
wrapped so generated outputs can naturally reach the mutation overlay.
"""
from __future__ import annotations

import copy
import ipaddress
import json
import random
import re
from functools import lru_cache
from pathlib import Path

_PRINTABLE_ASCII = [chr(code) for code in range(32, 127)]
_GRAMMAR_RULES_FILE: str | None = None


class Node:
    def generate(self, grammar: dict[str, "Node"], rng: random.Random) -> str:
        raise NotImplementedError

    def clone(self) -> "Node":
        return copy.deepcopy(self)

    def mutate(self, grammar: dict[str, "Node"], rng: random.Random) -> "Node":
        return self


class Literal(Node):
    def __init__(self, value: str) -> None:
        self.value = value

    def generate(self, grammar: dict[str, Node], rng: random.Random) -> str:
        return self.value

    def mutate(self, grammar: dict[str, Node], rng: random.Random) -> Node:
        if not self.value:
            return self
        action = rng.choice(["flip", "insert", "delete"])
        index = rng.randrange(len(self.value))
        if action == "flip":
            repl = rng.choice(_PRINTABLE_ASCII)
            self.value = self.value[:index] + repl + self.value[index + 1 :]
        elif action == "insert":
            repl = rng.choice(_PRINTABLE_ASCII)
            self.value = self.value[:index] + repl + self.value[index:]
        elif len(self.value) > 1:
            self.value = self.value[:index] + self.value[index + 1 :]
        return self


class CharClass(Node):
    def __init__(self, chars: list[str]) -> None:
        self.chars = chars

    def generate(self, grammar: dict[str, Node], rng: random.Random) -> str:
        if not self.chars:
            return ""
        return rng.choice(self.chars)

    def mutate(self, grammar: dict[str, Node], rng: random.Random) -> Node:
        action = rng.choice(["expand", "shrink", "shuffle"])
        if action == "expand":
            extra = rng.choice(_PRINTABLE_ASCII)
            if extra not in self.chars:
                self.chars.append(extra)
        elif action == "shrink" and len(self.chars) > 1:
            self.chars.pop(rng.randrange(len(self.chars)))
        elif len(self.chars) > 1:
            rng.shuffle(self.chars)
        return self


class Sequence(Node):
    def __init__(self, nodes: list[Node]) -> None:
        self.nodes = nodes

    def generate(self, grammar: dict[str, Node], rng: random.Random) -> str:
        return "".join(node.generate(grammar, rng) for node in self.nodes)

    def mutate(self, grammar: dict[str, Node], rng: random.Random) -> Node:
        if not self.nodes:
            return self
        action = rng.choice(["swap", "delete", "duplicate", "mutate_child"])
        if action == "swap" and len(self.nodes) > 1:
            i, j = rng.sample(range(len(self.nodes)), 2)
            self.nodes[i], self.nodes[j] = self.nodes[j], self.nodes[i]
        elif action == "delete" and len(self.nodes) > 1:
            self.nodes.pop(rng.randrange(len(self.nodes)))
        elif action == "duplicate":
            index = rng.randrange(len(self.nodes))
            self.nodes.insert(index, self.nodes[index].clone())
        else:
            rng.choice(self.nodes).mutate(grammar, rng)
        return self


class NumberRange(Node):
    def __init__(self, min_val: float, max_val: float, *, is_int: bool = True) -> None:
        self.min_val = min_val
        self.max_val = max_val
        self.is_int = is_int

    def generate(self, grammar: dict[str, Node], rng: random.Random) -> str:
        if self.is_int:
            low = int(self.min_val)
            high = int(self.max_val)
            if low > high:
                low, high = high, low
            return str(rng.randint(low, high))
        low = min(self.min_val, self.max_val)
        high = max(self.min_val, self.max_val)
        return str(rng.uniform(low, high))

    def mutate(self, grammar: dict[str, Node], rng: random.Random) -> Node:
        if not self.is_int:
            return self
        action = rng.choice(["shift", "widen", "shrink"])
        if action == "shift":
            delta = rng.randint(-8, 8)
            self.min_val += delta
            self.max_val += delta
        elif action == "widen":
            self.min_val -= rng.randint(0, 4)
            self.max_val += rng.randint(0, 8)
        else:
            self.max_val = max(self.min_val, self.max_val - rng.randint(0, 4))
        return self


class Alternation(Node):
    def __init__(self, options: list[Node]) -> None:
        self.options = options

    def generate(self, grammar: dict[str, Node], rng: random.Random) -> str:
        if not self.options:
            return ""
        return rng.choice(self.options).generate(grammar, rng)

    def mutate(self, grammar: dict[str, Node], rng: random.Random) -> Node:
        if not self.options:
            return self
        action = rng.choice(["mutate_branch", "swap", "duplicate"])
        if action == "mutate_branch":
            rng.choice(self.options).mutate(grammar, rng)
        elif action == "swap" and len(self.options) > 1:
            i, j = rng.sample(range(len(self.options)), 2)
            self.options[i], self.options[j] = self.options[j], self.options[i]
        else:
            option = rng.choice(self.options).clone()
            self.options.insert(rng.randrange(len(self.options) + 1), option)
        return self


class Repeat(Node):
    def __init__(self, node: Node, min_r: int, max_r: int) -> None:
        self.node = node
        self.min_r = min_r
        self.max_r = max_r

    def generate(self, grammar: dict[str, Node], rng: random.Random) -> str:
        low = min(self.min_r, self.max_r)
        high = max(self.min_r, self.max_r)
        count = rng.randint(low, high)
        return "".join(self.node.generate(grammar, rng) for _ in range(count))

    def mutate(self, grammar: dict[str, Node], rng: random.Random) -> Node:
        action = rng.choice(["widen", "shrink", "mutate_child"])
        if action == "widen":
            self.max_r += rng.randint(1, 3)
        elif action == "shrink":
            self.min_r = max(0, self.min_r - rng.randint(0, 1))
            self.max_r = max(self.min_r, self.max_r - rng.randint(0, 2))
        else:
            self.node.mutate(grammar, rng)
        return self


class Ref(Node):
    def __init__(self, name: str) -> None:
        self.name = name

    def generate(self, grammar: dict[str, Node], rng: random.Random) -> str:
        if self.name not in grammar:
            return ""
        return grammar[self.name].generate(grammar, rng)

    def mutate(self, grammar: dict[str, Node], rng: random.Random) -> Node:
        if self.name in grammar:
            grammar[self.name].mutate(grammar, rng)
        return self


def _parse_char_class(spec: str) -> CharClass:
    chars: list[str] = []
    index = 0
    while index < len(spec):
        if index + 2 < len(spec) and spec[index + 1] == "-":
            chars.extend(chr(code) for code in range(ord(spec[index]), ord(spec[index + 2]) + 1))
            index += 3
        else:
            chars.append(spec[index])
            index += 1
    return CharClass(chars)


def tokenize(expr: str) -> list[tuple[str, str]]:
    token_spec = [
        ("STRING", r'"(?:\\.|[^"\\])*"'),
        ("NUMRANGE", r"<number_range\s+min=[^ >]+\s+max=[^>]+>"),
        ("CLASS", r"\[[^\]]+\]"),
        ("REPEAT", r"\{\d+(,\d+)?\}"),
        ("REF", r"<[A-Za-z_][A-Za-z0-9_]*>"),
        ("ALT", r"\|"),
        ("LPAREN", r"\("),
        ("RPAREN", r"\)"),
        ("SKIP", r"\s+"),
    ]
    regex = "|".join(f"(?P<{name}>{pattern})" for name, pattern in token_spec)
    tokens: list[tuple[str, str]] = []
    position = 0
    for match in re.finditer(regex, expr):
        if match.start() != position:
            raise ValueError(f"Tokenizer skipped input at: {expr[position:match.start()]}")
        if match.lastgroup != "SKIP":
            tokens.append((match.lastgroup or "", match.group()))
        position = match.end()
    if position != len(expr):
        raise ValueError(f"Tokenizer stopped early at: {expr[position:]}")
    return tokens


def parse(expr: str) -> Node:
    tokens = tokenize(expr)
    position = 0

    def parse_expr() -> Node:
        nonlocal position
        nodes = [parse_sequence()]
        while position < len(tokens) and tokens[position][0] == "ALT":
            position += 1
            nodes.append(parse_sequence())
        return nodes[0] if len(nodes) == 1 else Alternation(nodes)

    def parse_sequence() -> Node:
        nonlocal position
        seq: list[Node] = []
        while position < len(tokens) and tokens[position][0] not in {"RPAREN", "ALT"}:
            seq.append(parse_term())
        if not seq:
            return Literal("")
        return seq[0] if len(seq) == 1 else Sequence(seq)

    def parse_term() -> Node:
        nonlocal position
        token_type, raw = tokens[position]
        position += 1

        if token_type == "STRING":
            node: Node = Literal(raw[1:-1])
        elif token_type == "CLASS":
            node = _parse_char_class(raw[1:-1])
        elif token_type == "REF":
            node = Ref(raw[1:-1])
        elif token_type == "NUMRANGE":
            match = re.fullmatch(r"<number_range\s+min=([^\s]+)\s+max=([^\s]+)>", raw)
            if match is None:
                raise ValueError(f"invalid number_range token {raw!r}")
            min_val = float(match.group(1))
            max_val = float(match.group(2))
            node = NumberRange(min_val, max_val, is_int=True)
        elif token_type == "LPAREN":
            node = parse_expr()
            if position >= len(tokens) or tokens[position][0] != "RPAREN":
                raise ValueError("Missing closing parenthesis")
            position += 1
        else:
            raise ValueError(f"Unsupported token {raw!r}")

        if position < len(tokens) and tokens[position][0] == "REPEAT":
            repeat = tokens[position][1][1:-1].split(",")
            position += 1
            if len(repeat) == 1:
                amount = int(repeat[0])
                node = Repeat(node, amount, amount)
            else:
                node = Repeat(node, int(repeat[0]), int(repeat[1]))
        return node

    return parse_expr()


class Grammar:
    def __init__(self) -> None:
        self.rules: dict[str, Node] = {}

    def add(self, name: str, expr: str) -> None:
        self.rules[name] = parse(expr)

    def generate(self, start: str, rng: random.Random) -> str:
        if start not in self.rules:
            raise KeyError(f"unknown grammar start rule {start!r}")
        return self.rules[start].generate(self.rules, rng)

    def mutate(
        self,
        rng: random.Random,
        *,
        preferred_rule_names: list[str] | None = None,
    ) -> None:
        preferred_nodes = [
            self.rules[name]
            for name in (preferred_rule_names or [])
            if name in self.rules
        ]
        pool = preferred_nodes or list(self.rules.values())
        if not pool:
            return
        rng.choice(pool).mutate(self.rules, rng)


def build() -> Grammar:
    grammar = Grammar()

    grammar.add("dot", '"."')
    grammar.add("slash", '"/"')
    grammar.add("digit", "[0-9]")
    grammar.add("letter", "[a-zA-Z]")
    grammar.add("quote", '["]')

    grammar.add(
        "octet",
        '("25"[0-5] | "2"[0-4]<digit> | "1"<digit><digit> | <digit>{1,2})',
    )
    grammar.add("ipv4", "<octet> <dot> <octet> <dot> <octet> <dot> <octet>")
    grammar.add("cidr4", "<number_range min=0 max=32>")
    grammar.add("ipv4_cidr", "<ipv4> <slash> <cidr4>")

    grammar.add("hex", "[0-9a-f]{1,4}")
    grammar.add(
        "ipv6",
        '<hex> ":" <hex> ":" <hex> ":" <hex> ":" <hex> ":" <hex> ":" <hex> ":" <hex>',
    )
    grammar.add("cidr6", "<number_range min=0 max=128>")
    grammar.add("ipv6_cidr", '<ipv6> "/" <cidr6>')
    grammar.add("ip", "<ipv4_cidr> | <ipv6_cidr> | <ipv4> | <ipv6>")

    grammar.add("string", '<quote> (<letter>|<digit>|"_"|"-"){1,8} <quote>')
    grammar.add("number", '("-" <digit>{1,3}) | <digit>{1,3}')
    grammar.add("bool", '"true" | "false"')
    grammar.add("null", '"null"')
    grammar.add("scalar", "<string> | <number> | <bool> | <null>")
    grammar.add("pair", '<string> ":" <scalar>')
    grammar.add("object", '"{}" | "{" <pair> ("," <pair>){0,3} "}"')
    grammar.add("array", '"[]" | "[" <scalar> ("," <scalar>){0,3} "]"')
    grammar.add("value", "<string> | <number> | <array>")

    return grammar


def configure(*, grammar_rules_file: str | None) -> None:
    global _GRAMMAR_RULES_FILE
    _GRAMMAR_RULES_FILE = grammar_rules_file
    _base_grammar.cache_clear()


def _parse_rule_definition(line: str) -> tuple[str, str]:
    if "::=" in line:
        name, expr = line.split("::=", 1)
    elif ":=" in line:
        name, expr = line.split(":=", 1)
    elif "=" in line:
        name, expr = line.split("=", 1)
    else:
        raise ValueError(f"invalid grammar rule line: {line!r}")
    return name.strip(), expr.strip()


def _apply_extra_rules(grammar: Grammar) -> None:
    if not _GRAMMAR_RULES_FILE:
        return
    path = Path(_GRAMMAR_RULES_FILE)
    with path.open(encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            name, expr = _parse_rule_definition(line)
            grammar.add(name, expr)


def _link_mutation_overlays(grammar: Grammar) -> None:
    for overlay_name in [name for name in grammar.rules if name.endswith("_mut")]:
        base_name = overlay_name[:-4]
        if base_name not in grammar.rules:
            continue
        original = grammar.rules[base_name]
        grammar.rules[base_name] = Alternation([Ref(overlay_name), original])


@lru_cache(maxsize=1)
def _base_grammar() -> Grammar:
    grammar = build()
    _apply_extra_rules(grammar)
    _link_mutation_overlays(grammar)
    return grammar


def _expand_preferred_rules(grammar: Grammar, names: list[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for name in names:
        for candidate in (name, f"{name}_mut"):
            if candidate in grammar.rules and candidate not in seen:
                ordered.append(candidate)
                seen.add(candidate)
    return ordered


def _choose_ip_profile(text: str, grammar: Grammar, rng: random.Random) -> tuple[str, list[str]]:
    stripped = text.strip()
    detected: str
    if not stripped:
        detected = rng.choice(["ipv4_cidr", "ipv6_cidr"])
    else:
        try:
            if "/" in stripped:
                address_text, _prefix = stripped.rsplit("/", 1)
                try:
                    ipaddress.IPv4Address(address_text)
                    detected = "ipv4_cidr"
                except ValueError:
                    ipaddress.IPv6Address(address_text)
                    detected = "ipv6_cidr"
            else:
                try:
                    ipaddress.IPv4Address(stripped)
                    detected = "ipv4"
                except ValueError:
                    ipaddress.IPv6Address(stripped)
                    detected = "ipv6"
        except ValueError:
            if ":" in stripped:
                detected = "ipv6_cidr" if "/" in stripped else "ipv6"
            elif "." in stripped:
                detected = "ipv4_cidr" if "/" in stripped else "ipv4"
            else:
                detected = "ip"

    start_candidates = {
        "ipv4": ["ipv4", "ipv4_start", "ip_start", "ip"],
        "ipv4_cidr": ["ipv4_cidr", "ipv4_start", "ip_start", "ip"],
        "ipv6": ["ipv6", "ipv6_start", "ip_start", "ip"],
        "ipv6_cidr": ["ipv6_cidr", "ipv6_start", "ip_start", "ip"],
        "ip": ["ip_start", "ip", "ipv4_cidr", "ipv6_cidr"],
    }
    preferred_candidates = {
        "ipv4": ["ipv4", "octet", "dot"],
        "ipv4_cidr": ["ipv4_cidr", "ipv4", "octet", "dot", "cidr4", "slash"],
        "ipv6": ["ipv6", "hex"],
        "ipv6_cidr": ["ipv6_cidr", "ipv6", "hex", "cidr6", "slash"],
        "ip": ["ip", "ipv4_cidr", "ipv6_cidr", "octet", "hex", "cidr4", "cidr6"],
    }
    start_rule = next(
        (name for name in start_candidates[detected] if name in grammar.rules),
        "ip" if "ip" in grammar.rules else next(iter(grammar.rules)),
    )
    preferred = _expand_preferred_rules(grammar, preferred_candidates[detected])
    return start_rule, preferred


def _choose_json_profile(text: str, grammar: Grammar) -> tuple[str, list[str]]:
    stripped = text.strip()
    parsed: object | None
    if not stripped:
        detected = "object"
        preferred_names = ["object", "pair", "string", "scalar", "number", "bool", "null"]
        start_rule = next(
            (name for name in ["json_start", "object", "value"] if name in grammar.rules),
            "object" if "object" in grammar.rules else next(iter(grammar.rules)),
        )
        preferred = _expand_preferred_rules(grammar, preferred_names)
        return start_rule, preferred
    try:
        parsed = json.loads(stripped) if stripped else None
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        detected = "object"
        preferred_names = ["object", "pair", "string", "scalar", "number", "bool", "null"]
    elif isinstance(parsed, list):
        detected = "array"
        preferred_names = ["array", "scalar", "string", "number", "bool", "null"]
    elif isinstance(parsed, str):
        detected = "string"
        preferred_names = ["value", "scalar", "string"]
    elif isinstance(parsed, bool):
        detected = "bool"
        preferred_names = ["value", "scalar", "bool"]
    elif parsed is None and stripped == "null":
        detected = "null"
        preferred_names = ["value", "scalar", "null"]
    elif isinstance(parsed, (int, float)):
        detected = "number"
        preferred_names = ["value", "scalar", "number"]
    else:
        if stripped.startswith("{"):
            detected = "object"
            preferred_names = ["object", "pair", "string", "scalar"]
        elif stripped.startswith("["):
            detected = "array"
            preferred_names = ["array", "scalar", "string", "number"]
        else:
            detected = "value"
            preferred_names = ["value", "scalar", "string", "number", "bool", "null"]

    start_candidates = {
        "object": ["object", "json_start", "value"],
        "array": ["array", "json_start", "value"],
        "string": ["string", "value", "json_start"],
        "number": ["number", "value", "json_start"],
        "bool": ["bool", "value", "json_start"],
        "null": ["null", "value", "json_start"],
        "value": ["json_start", "value", "object"],
    }
    start_rule = next(
        (name for name in start_candidates[detected] if name in grammar.rules),
        "object" if "object" in grammar.rules else next(iter(grammar.rules)),
    )
    preferred = _expand_preferred_rules(grammar, preferred_names)
    return start_rule, preferred


def _choose_profile(
    *,
    text: str,
    mutator_kind: str,
    grammar: Grammar,
    rng: random.Random,
) -> tuple[str, list[str]]:
    if mutator_kind == "ip":
        return _choose_ip_profile(text, grammar, rng)
    return _choose_json_profile(text, grammar)


def _blend_with_seed(
    *,
    original_text: str,
    generated_text: str,
    rng: random.Random,
) -> str:
    if not original_text:
        return generated_text
    if not generated_text:
        return original_text
    strategy = rng.choice(["generated", "replace_slice", "insert_generated"])
    if strategy == "generated" or len(original_text) < 2:
        return generated_text
    if strategy == "replace_slice":
        start = rng.randrange(len(original_text))
        end = rng.randrange(start, len(original_text) + 1)
        return original_text[:start] + generated_text + original_text[end:]
    insert_at = rng.randrange(len(original_text) + 1)
    return original_text[:insert_at] + generated_text + original_text[insert_at:]


def generate_without_seed(
    *,
    mutator_kind: str,
    rng: random.Random,
    count: int = 1,
) -> list[str]:
    if count <= 0:
        return []
    base = _base_grammar()
    outputs: list[str] = []
    seen: set[str] = set()
    attempts = 0
    max_attempts = max(count * 10, 25)
    while len(outputs) < count and attempts < max_attempts:
        attempts += 1
        grammar = copy.deepcopy(base)
        start_rule, preferred = _choose_profile(
            text="",
            mutator_kind=mutator_kind,
            grammar=grammar,
            rng=rng,
        )
        for _round in range(rng.randint(0, 3)):
            grammar.mutate(rng, preferred_rule_names=preferred)
        generated = grammar.generate(start_rule, rng)
        if generated in seen:
            continue
        seen.add(generated)
        outputs.append(generated)
    return outputs


def generate_from_rule(
    *,
    start_rule: str,
    rng: random.Random,
    count: int = 1,
    min_mutation_rounds: int = 0,
    max_mutation_rounds: int = 3,
    preferred_rule_names: list[str] | None = None,
) -> list[str]:
    if count <= 0:
        return []
    if min_mutation_rounds < 0 or max_mutation_rounds < min_mutation_rounds:
        raise ValueError("invalid mutation round bounds")
    base = _base_grammar()
    if start_rule not in base.rules:
        raise KeyError(f"unknown grammar start rule {start_rule!r}")
    preferred = _expand_preferred_rules(base, preferred_rule_names or [])
    outputs: list[str] = []
    seen: set[str] = set()
    attempts = 0
    max_attempts = max(count * 10, 25)
    while len(outputs) < count and attempts < max_attempts:
        attempts += 1
        grammar = copy.deepcopy(base)
        for _round in range(rng.randint(min_mutation_rounds, max_mutation_rounds)):
            grammar.mutate(rng, preferred_rule_names=preferred)
        generated = grammar.generate(start_rule, rng)
        if generated in seen:
            continue
        seen.add(generated)
        outputs.append(generated)
    return outputs


def mutate_from_rule(
    text: str,
    *,
    start_rule: str,
    rng: random.Random,
    preferred_rule_names: list[str] | None = None,
    min_mutation_rounds: int = 1,
    max_mutation_rounds: int = 5,
    blend_with_seed: bool = True,
) -> str:
    if min_mutation_rounds < 0 or max_mutation_rounds < min_mutation_rounds:
        raise ValueError("invalid mutation round bounds")
    base = _base_grammar()
    if start_rule not in base.rules:
        raise KeyError(f"unknown grammar start rule {start_rule!r}")
    preferred = _expand_preferred_rules(base, preferred_rule_names or [])
    grammar = copy.deepcopy(base)
    for _ in range(rng.randint(min_mutation_rounds, max_mutation_rounds)):
        grammar.mutate(rng, preferred_rule_names=preferred)
    generated = grammar.generate(start_rule, rng)
    if blend_with_seed and text:
        return _blend_with_seed(original_text=text, generated_text=generated, rng=rng)
    return generated


def mutate(
    text: str,
    *,
    mutator_kind: str,
    rng: random.Random,
) -> str:
    base = _base_grammar()
    grammar = copy.deepcopy(base)
    start_rule, preferred = _choose_profile(
        text=text,
        mutator_kind=mutator_kind,
        grammar=grammar,
        rng=rng,
    )
    mutation_rounds = rng.randint(1, 5)
    for _ in range(mutation_rounds):
        grammar.mutate(rng, preferred_rule_names=preferred)
    generated = grammar.generate(start_rule, rng)
    if text:
        return _blend_with_seed(original_text=text, generated_text=generated, rng=rng)
    return generated


__all__ = [
    "Grammar",
    "Node",
    "Literal",
    "CharClass",
    "Sequence",
    "Alternation",
    "Repeat",
    "NumberRange",
    "Ref",
    "build",
    "configure",
    "generate_from_rule",
    "generate_without_seed",
    "mutate",
    "mutate_from_rule",
    "parse",
    "tokenize",
]
