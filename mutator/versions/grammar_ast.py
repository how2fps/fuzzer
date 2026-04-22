"""
Generalized grammar-AST mutator inspired by mutator_test.py.

This version keeps the grammar DSL and generic node-type mutation operators:
Literal, CharClass, Sequence, Alternation, Repeat, NumberRange, and Ref.

The seed is used to:
- choose a sensible start rule from grammar structure
- bias which grammar rules are mutated more often
- optionally blend the generated candidate back with the original seed

Extra rules from ``-g/--grammar-rules-file`` are parsed into the same grammar.
If a rule name ends with ``_mut`` and its base rule exists, the base rule is
wrapped so generated outputs can naturally reach the mutation overlay.
"""
from __future__ import annotations

import copy
import json
import random
import re
from collections.abc import Sequence as SequenceCollection
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .lib import resolve_ast_grammar_spec, runtime_grammar_version

_PRINTABLE_ASCII = [chr(code) for code in range(32, 127)]
_GRAMMAR_RULES_FILE: str | None = None
_DEFAULT_MAX_GENERATION_DEPTH = 5
_MAX_PARTIAL_PARSE_POSITIONS = 192
_MAX_REPEAT_BOUNDARY_COUNT = 64


def _normalize_rule_name(name: str) -> str:
    if name.startswith("<") and name.endswith(">") and len(name) >= 3:
        return name[1:-1]
    return name


class Node:
    """Base class for grammar AST nodes used by the DSL parser."""

    def generate(self, grammar: dict[str, "Node"], rng: random.Random) -> str:
        raise NotImplementedError

    def clone(self) -> "Node":
        return copy.deepcopy(self)

    def mutate(self, grammar: dict[str, "Node"], rng: random.Random) -> "Node":
        return self


class SeedTreeNode:
    """Base class for parsed seed trees that can be mutated and serialized."""

    def mutate_self(self, rng: random.Random, grammar: "Grammar") -> "SeedTreeNode":
        return self

    def walk_mutable_nodes(self) -> list["SeedTreeNode"]:
        return [self]

    def to_text(self) -> str:
        raise NotImplementedError


@dataclass
class ParseTreeNode(SeedTreeNode):
    """Matched derivation-tree node for grammar-driven seed parsing.

    This is the generic bridge between:
    - a concrete seed string
    - the grammar rule tree that recognized it

    Each node records the grammar-node kind that matched the seed text and any
    child derivation nodes. Mutations operate on these parsed tree nodes rather
    than on raw text, which makes add/delete/replace behaviors readable and
    grammar-aware for any external rule file.
    """

    kind: str
    text: str
    children: list["ParseTreeNode"] = field(default_factory=list)
    ref_name: str | None = None
    grammar_node: Node | None = None
    choice_index: int | None = None

    def walk_mutable_nodes(self) -> list[SeedTreeNode]:
        nodes: list[SeedTreeNode] = [self]
        for child in self.children:
            nodes.extend(child.walk_mutable_nodes())
        return nodes

    def mutate_self(self, rng: random.Random, grammar: "Grammar") -> SeedTreeNode:
        """Apply a generic node-type mutation to one parsed derivation node."""
        if self.kind == "literal":
            literal = Literal(self.text)
            literal.mutate(grammar.rules, rng)
            self.text = literal.value
            return self
        if self.kind == "charclass":
            if self.grammar_node is not None and isinstance(self.grammar_node, CharClass):
                self.text = self.grammar_node.generate(grammar.rules, rng)
            elif self.text:
                self.text = rng.choice(_PRINTABLE_ASCII)
            return self
        if self.kind == "numberrange":
            self.text = _mutate_numberrange_text(self.text, self.grammar_node, rng)
            return self
        if self.kind == "sequence":
            _mutate_sequence_parse_node(self, rng, grammar)
            return self
        if self.kind == "alternation":
            _mutate_alternation_parse_node(self, rng, grammar)
            return self
        if self.kind == "repeat":
            _mutate_repeat_parse_node(self, rng, grammar)
            return self
        if self.kind == "ref":
            _mutate_ref_parse_node(self, rng, grammar)
            return self
        if self.children:
            target = rng.choice(self.children)
            target.mutate_self(rng, grammar)
            self.text = self.to_text()
        return self

    def to_text(self) -> str:
        """Serialize the matched parse subtree back into text."""
        if self.kind in {"literal", "charclass", "numberrange"}:
            return self.text
        if self.kind in {"sequence", "repeat"}:
            return "".join(child.to_text() for child in self.children)
        if self.kind in {"alternation", "ref"}:
            return self.children[0].to_text() if self.children else self.text
        return self.text


@dataclass(frozen=True)
class PartialParseMatch:
    """Best-effort partial match used to salvage structure from invalid seeds."""

    rule_name: str
    tree: ParseTreeNode
    start: int
    end: int

class Literal(Node):
    """A fixed terminal string in the grammar."""
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
    """A single-character choice drawn from a character class like [0-9]."""
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
    """An ordered list of child nodes that generate or match in sequence."""
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
    """A numeric range generator such as <number_range min=0 max=32>."""
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
    """A one-of-many choice node created from the '|' operator."""
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
    """A repeated child node created from {m,n} syntax."""
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
    """A reference to another named grammar rule, e.g. <digit>."""
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
    """Expand a bracket expression like 0-9 into an explicit CharClass."""
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
    """Tokenize one DSL expression into parser-friendly token tuples."""
    token_spec = [
        ("STRING", r'"(?:\\.|[^"\\])*"'),
        ("NUMRANGE", r"<number_range\s+min=[^ >]+\s+max=[^>]+>"),
        ("CLASS", r"\[[^\]]+\]"),
        ("REPEAT", r"\{\d+(,\d+)?\}"),
        ("REF", r"<[^<>]+>"),
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
    """Parse one DSL expression string into a grammar AST node."""
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
            node = Literal(json.loads(raw))
        elif token_type == "CLASS":
            node = _parse_char_class(raw[1:-1])
        elif token_type == "REF":
            node = Ref(_normalize_rule_name(raw))
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


class _GenerationError(RuntimeError):
    pass


class Grammar:
    """Container for named grammar rules plus generation/mutation helpers."""

    def __init__(
        self,
        *,
        start_rule: str | None = None,
        recursive_rule_names: set[str] | None = None,
    ) -> None:
        self.rules: dict[str, Node] = {}
        self.start_rule = (
            _normalize_rule_name(start_rule) if start_rule is not None else None
        )
        self.recursive_rule_names = {
            _normalize_rule_name(name) for name in (recursive_rule_names or set())
        }

    def add(self, name: str, expr: str) -> None:
        self.rules[_normalize_rule_name(name)] = parse(expr)

    def _generate_rule(
        self,
        name: str,
        rng: random.Random,
        *,
        active_counts: dict[str, int],
        max_depth: int,
        preferred_coverage_items: set[str] | None = None,
    ) -> str:
        normalized_name = _normalize_rule_name(name)
        if normalized_name not in self.rules:
            raise KeyError(f"unknown grammar start rule {normalized_name!r}")

        next_counts = active_counts
        if normalized_name in self.recursive_rule_names:
            current_depth = active_counts.get(normalized_name, 0)
            if current_depth >= max_depth:
                raise _GenerationError(f"generation depth exceeded for {normalized_name!r}")
            next_counts = dict(active_counts)
            next_counts[normalized_name] = current_depth + 1

        return self._generate_node(
            self.rules[normalized_name],
            rng,
            active_counts=next_counts,
            max_depth=max_depth,
            preferred_coverage_items=preferred_coverage_items,
            current_rule_name=normalized_name,
        )

    def _generate_node(
        self,
        node: Node,
        rng: random.Random,
        *,
        active_counts: dict[str, int],
        max_depth: int,
        preferred_coverage_items: set[str] | None = None,
        current_rule_name: str | None = None,
    ) -> str:
        if isinstance(node, Literal):
            return node.value
        if isinstance(node, CharClass):
            if not node.chars:
                return ""
            return rng.choice(node.chars)
        if isinstance(node, NumberRange):
            low = min(node.min_val, node.max_val)
            high = max(node.min_val, node.max_val)
            if node.is_int:
                return str(rng.randint(int(low), int(high)))
            return str(rng.uniform(low, high))
        if isinstance(node, Sequence):
            return "".join(
                self._generate_node(
                    child,
                    rng,
                    active_counts=active_counts,
                    max_depth=max_depth,
                    preferred_coverage_items=preferred_coverage_items,
                    current_rule_name=current_rule_name,
                )
                for child in node.nodes
            )
        if isinstance(node, Alternation):
            options = list(node.options)
            if not options:
                return ""
            option_order = list(range(len(options)))
            if preferred_coverage_items:
                scored_options = []
                for index, option in enumerate(options):
                    score = _alternation_preference_score(
                        option=option,
                        option_index=index,
                        current_rule_name=current_rule_name,
                        grammar=self,
                        preferred_coverage_items=preferred_coverage_items,
                    )
                    scored_options.append((score, rng.random(), index))
                option_order = [
                    index
                    for _score, _tie_breaker, index in sorted(
                        scored_options,
                        key=lambda item: (-item[0], item[1]),
                    )
                ]
            else:
                option_order = rng.sample(option_order, k=len(option_order))
            for option_index in option_order:
                option = options[option_index]
                try:
                    return self._generate_node(
                        option,
                        rng,
                        active_counts=active_counts,
                        max_depth=max_depth,
                        preferred_coverage_items=preferred_coverage_items,
                        current_rule_name=current_rule_name,
                    )
                except _GenerationError:
                    continue
            raise _GenerationError("no viable alternation branch")
        if isinstance(node, Repeat):
            low = min(node.min_r, node.max_r)
            high = max(node.min_r, node.max_r)
            counts = list(range(low, high + 1))
            if preferred_coverage_items and _node_matches_preferred_coverage(
                node=node.node,
                grammar=self,
                preferred_coverage_items=preferred_coverage_items,
            ):
                counts = sorted(
                    counts,
                    key=lambda item: (0 if item > 0 else 1, item, rng.random()),
                )
            else:
                counts = sorted(counts, key=lambda item: (item, rng.random()))
            for count in counts:
                try:
                    return "".join(
                        self._generate_node(
                            node.node,
                            rng,
                            active_counts=active_counts,
                            max_depth=max_depth,
                            preferred_coverage_items=preferred_coverage_items,
                            current_rule_name=current_rule_name,
                        )
                        for _ in range(count)
                    )
                except _GenerationError:
                    continue
            raise _GenerationError("no viable repeat count")
        if isinstance(node, Ref):
            if node.name not in self.rules:
                return ""
            return self._generate_rule(
                node.name,
                rng,
                active_counts=active_counts,
                max_depth=max_depth,
                preferred_coverage_items=preferred_coverage_items,
            )
        return ""

    def generate(
        self,
        start: str,
        rng: random.Random,
        *,
        max_depth: int = _DEFAULT_MAX_GENERATION_DEPTH,
        preferred_coverage_items: list[str] | None = None,
    ) -> str:
        return self._generate_rule(
            start,
            rng,
            active_counts={},
            max_depth=max_depth,
            preferred_coverage_items=(
                {_normalize_coverage_item_name(item) for item in preferred_coverage_items}
                if preferred_coverage_items
                else None
            ),
        )

    def generate_from_node(
        self,
        node: Node,
        rng: random.Random,
        *,
        max_depth: int = _DEFAULT_MAX_GENERATION_DEPTH,
        preferred_coverage_items: list[str] | None = None,
        current_rule_name: str | None = None,
    ) -> str:
        return self._generate_node(
            node,
            rng,
            active_counts={},
            max_depth=max_depth,
            preferred_coverage_items=(
                {_normalize_coverage_item_name(item) for item in preferred_coverage_items}
                if preferred_coverage_items
                else None
            ),
            current_rule_name=current_rule_name,
        )

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


def _ordered_unique_ints(values: SequenceCollection[int]) -> tuple[int, ...]:
    ordered: list[int] = []
    seen: set[int] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return tuple(ordered)


def _ordered_unique_strings(values: SequenceCollection[str]) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return tuple(ordered)


def _numberrange_boundary_candidates(
    text: str,
    grammar_node: Node | None,
) -> tuple[str, ...]:
    if not isinstance(grammar_node, NumberRange) or not grammar_node.is_int:
        return ()
    try:
        value = int(text)
    except ValueError:
        return ()

    low = int(min(grammar_node.min_val, grammar_node.max_val))
    high = int(max(grammar_node.min_val, grammar_node.max_val))
    digit_width = max(
        1,
        len(str(abs(value))),
        len(str(abs(low))),
        len(str(abs(high))),
    )
    amplified_width = min(max(digit_width + 1, 2), 18)
    amplified_value = int("9" * amplified_width)

    pivot = max(abs(value), abs(low), abs(high), 1)
    zero_padded = str(pivot).rjust(min(max(digit_width + 1, 2), 8), "0")
    stretched = str(pivot) + ("0" * min(4, max(1, digit_width // 2 or 1)))

    candidates = _ordered_unique_strings(
        (
            str(low),
            str(high),
            str(low - 1),
            str(high + 1),
            "0",
            "1",
            "-1",
            str(amplified_value),
            str(-amplified_value),
            zero_padded,
            stretched,
            str(abs(value)) if value < 0 else str(-abs(value)) if value > 0 else "",
            f"-{zero_padded}" if (low < 0 or value < 0) else "",
            f"-{stretched}" if (low < 0 or value < 0) else "",
        )
    )
    return tuple(candidate for candidate in candidates if candidate and candidate != text)


def _mutate_numberrange_text(
    text: str,
    grammar_node: Node | None,
    rng: random.Random,
) -> str:
    """Mutate a matched numeric range token while staying reasonably readable."""
    try:
        value = int(text)
    except ValueError:
        return text
    boundary_candidates = _numberrange_boundary_candidates(text, grammar_node)
    if boundary_candidates:
        return rng.choice(list(boundary_candidates))
    if isinstance(grammar_node, NumberRange) and grammar_node.is_int:
        low = int(min(grammar_node.min_val, grammar_node.max_val))
        high = int(max(grammar_node.min_val, grammar_node.max_val))
        return str(rng.randint(low, high))
    return str(value + rng.randint(-8, 8))


def _generate_parse_subtree_from_node(
    node: Node,
    grammar: Grammar,
    rng: random.Random,
) -> ParseTreeNode:
    """Generate and re-parse one grammar-node subtree for add/replace edits."""
    for _ in range(8):
        generated = grammar.generate_from_node(node, rng)
        parsed = _match_exact_node(node=node, text=generated, grammar=grammar)
        if parsed is not None:
            return parsed
    return ParseTreeNode(
        kind="literal",
        text=grammar.generate_from_node(node, rng),
        grammar_node=node,
    )


def _mutate_sequence_parse_node(
    node: ParseTreeNode,
    rng: random.Random,
    grammar: Grammar,
) -> None:
    """Mutate an ordered parsed sequence via child-level structural edits."""
    if not node.children:
        return
    action = rng.choice(["mutate_child", "swap", "delete", "duplicate"])
    if action == "mutate_child":
        rng.choice(node.children).mutate_self(rng, grammar)
    elif action == "swap" and len(node.children) > 1:
        i, j = rng.sample(range(len(node.children)), 2)
        node.children[i], node.children[j] = node.children[j], node.children[i]
    elif action == "delete" and len(node.children) > 1:
        node.children.pop(rng.randrange(len(node.children)))
    elif action == "duplicate":
        child = copy.deepcopy(rng.choice(node.children))
        node.children.insert(rng.randrange(len(node.children) + 1), child)
    node.text = node.to_text()


def _mutate_alternation_parse_node(
    node: ParseTreeNode,
    rng: random.Random,
    grammar: Grammar,
) -> None:
    """Mutate a matched alternation by switching branches or mutating the branch."""
    if node.grammar_node is None or not isinstance(node.grammar_node, Alternation):
        if node.children:
            rng.choice(node.children).mutate_self(rng, grammar)
        return
    action = rng.choice(["switch_branch", "mutate_child"])
    if action == "switch_branch" and node.grammar_node.options:
        option = rng.choice(node.grammar_node.options)
        node.children = [_generate_parse_subtree_from_node(option, grammar, rng)]
    elif node.children:
        node.children[0].mutate_self(rng, grammar)
    node.text = node.to_text()


def _mutate_repeat_parse_node(
    node: ParseTreeNode,
    rng: random.Random,
    grammar: Grammar,
) -> None:
    """Mutate a repetition node by adding, deleting, duplicating, or editing items."""
    if node.grammar_node is None or not isinstance(node.grammar_node, Repeat):
        if node.children:
            rng.choice(node.children).mutate_self(rng, grammar)
        return
    action = rng.choice(["add", "drop", "duplicate", "mutate_child", "amplify_boundary"])
    if action == "add":
        node.children.insert(
            rng.randrange(len(node.children) + 1),
            _generate_parse_subtree_from_node(node.grammar_node.node, grammar, rng),
        )
    elif action == "drop" and node.children:
        node.children.pop(rng.randrange(len(node.children)))
    elif action == "duplicate" and node.children:
        child = copy.deepcopy(rng.choice(node.children))
        node.children.insert(rng.randrange(len(node.children) + 1), child)
    elif action == "amplify_boundary":
        target_counts = _repeat_boundary_target_counts(
            node=node.grammar_node,
            current_count=len(node.children),
        )
        if target_counts:
            _resize_repeat_children(
                node=node,
                target_count=rng.choice(list(target_counts)),
                rng=rng,
                grammar=grammar,
            )
        elif node.children:
            rng.choice(node.children).mutate_self(rng, grammar)
        else:
            node.children.append(
                _generate_parse_subtree_from_node(node.grammar_node.node, grammar, rng)
            )
    elif node.children:
        rng.choice(node.children).mutate_self(rng, grammar)
    else:
        node.children.append(_generate_parse_subtree_from_node(node.grammar_node.node, grammar, rng))
    node.text = node.to_text()


def _mutate_ref_parse_node(
    node: ParseTreeNode,
    rng: random.Random,
    grammar: Grammar,
) -> None:
    """Mutate a rule reference by editing the child or regenerating the rule."""
    if node.ref_name is None or node.ref_name not in grammar.rules:
        if node.children:
            node.children[0].mutate_self(rng, grammar)
        return
    action = rng.choice(["mutate_child", "replace_rule"])
    if action == "replace_rule":
        replacement = _generate_parse_subtree_from_node(grammar.rules[node.ref_name], grammar, rng)
        node.children = [replacement]
    elif node.children:
        node.children[0].mutate_self(rng, grammar)
    node.text = node.to_text()


def _repeat_boundary_target_counts(
    *,
    node: Repeat,
    current_count: int,
) -> tuple[int, ...]:
    low = min(node.min_r, node.max_r)
    high = max(node.min_r, node.max_r)
    hard_cap = min(
        _MAX_REPEAT_BOUNDARY_COUNT,
        max(high + 1, current_count + 1, current_count * 4 if current_count else 0, 8),
    )
    candidates = _ordered_unique_ints(
        (
            max(0, low - 1),
            low,
            min(high, hard_cap),
            min(high + 1, hard_cap),
            max(0, current_count - 1),
            min(current_count + 1, hard_cap),
            min(max(current_count * 2, current_count + 2), hard_cap),
            min(max(current_count * 4, current_count + 4), hard_cap),
            *(bucket for bucket in (0, 1, 2, 3, 4, 8, 16, 32, 64) if bucket <= hard_cap),
        )
    )
    return tuple(count for count in candidates if count != current_count)


def _resize_repeat_children(
    *,
    node: ParseTreeNode,
    target_count: int,
    rng: random.Random,
    grammar: Grammar,
) -> None:
    if not isinstance(node.grammar_node, Repeat):
        return
    while node.children and len(node.children) > target_count:
        node.children.pop(rng.randrange(len(node.children)))
    while len(node.children) < target_count:
        node.children.insert(
            rng.randrange(len(node.children) + 1),
            _generate_parse_subtree_from_node(node.grammar_node.node, grammar, rng),
        )


def _match_exact_node(*, node: Node, text: str, grammar: Grammar) -> ParseTreeNode | None:
    """Return a matched parse tree only when the node consumes the whole text."""
    matches = _match_node(node=node, text=text, pos=0, grammar=grammar, memo={})
    for matched, end in matches:
        if end == len(text):
            return matched
    return None


def _match_node(
    *,
    node: Node,
    text: str,
    pos: int,
    grammar: Grammar,
    memo: dict[tuple[int, int], list[tuple[ParseTreeNode, int]]],
) -> list[tuple[ParseTreeNode, int]]:
    """Match one grammar AST node against the seed text starting at ``pos``."""
    key = (id(node), pos)
    if key in memo:
        return memo[key]

    results: list[tuple[ParseTreeNode, int]] = []
    if isinstance(node, Literal):
        if text.startswith(node.value, pos):
            end = pos + len(node.value)
            results.append((ParseTreeNode(kind="literal", text=text[pos:end], grammar_node=node), end))
    elif isinstance(node, CharClass):
        if pos < len(text) and text[pos] in node.chars:
            end = pos + 1
            results.append((ParseTreeNode(kind="charclass", text=text[pos:end], grammar_node=node), end))
    elif isinstance(node, NumberRange):
        match = re.match(r"-?\d+", text[pos:])
        if match is not None:
            candidate = match.group()
            try:
                value = int(candidate)
            except ValueError:
                value = None
            if value is not None:
                low = int(min(node.min_val, node.max_val))
                high = int(max(node.min_val, node.max_val))
                if low <= value <= high:
                    end = pos + len(candidate)
                    results.append((ParseTreeNode(kind="numberrange", text=candidate, grammar_node=node), end))
    elif isinstance(node, Ref):
        if node.name in grammar.rules:
            for child, end in _match_node(
                node=grammar.rules[node.name],
                text=text,
                pos=pos,
                grammar=grammar,
                memo=memo,
            ):
                results.append(
                    (
                        ParseTreeNode(
                            kind="ref",
                            text=text[pos:end],
                            children=[child],
                            ref_name=node.name,
                            grammar_node=node,
                        ),
                        end,
                    )
                )
    elif isinstance(node, Sequence):
        partials: list[tuple[list[ParseTreeNode], int]] = [([], pos)]
        for child_node in node.nodes:
            next_partials: list[tuple[list[ParseTreeNode], int]] = []
            for children, child_pos in partials:
                for matched_child, end in _match_node(
                    node=child_node,
                    text=text,
                    pos=child_pos,
                    grammar=grammar,
                    memo=memo,
                ):
                    next_partials.append((children + [matched_child], end))
            partials = next_partials
            if not partials:
                break
        for children, end in partials:
            results.append(
                (
                    ParseTreeNode(
                        kind="sequence",
                        text=text[pos:end],
                        children=children,
                        grammar_node=node,
                    ),
                    end,
                )
            )
    elif isinstance(node, Alternation):
        for option_index, option in enumerate(node.options):
            for matched_child, end in _match_node(
                node=option,
                text=text,
                pos=pos,
                grammar=grammar,
                memo=memo,
            ):
                results.append(
                    (
                        ParseTreeNode(
                            kind="alternation",
                            text=text[pos:end],
                            children=[matched_child],
                            grammar_node=node,
                            choice_index=option_index,
                        ),
                        end,
                    )
                )
    elif isinstance(node, Repeat):
        results.extend(_match_repeat_node(node=node, text=text, pos=pos, grammar=grammar, memo=memo))

    memo[key] = results
    return results


def _match_repeat_node(
    *,
    node: Repeat,
    text: str,
    pos: int,
    grammar: Grammar,
    memo: dict[tuple[int, int], list[tuple[ParseTreeNode, int]]],
) -> list[tuple[ParseTreeNode, int]]:
    """Backtracking matcher for repeated grammar nodes like X{m,n}."""
    results: list[tuple[ParseTreeNode, int]] = []
    max_count = max(node.min_r, node.max_r)

    def dfs(current_pos: int, depth: int, children: list[ParseTreeNode]) -> None:
        if depth >= node.min_r:
            results.append(
                (
                    ParseTreeNode(
                        kind="repeat",
                        text=text[pos:current_pos],
                        children=copy.deepcopy(children),
                        grammar_node=node,
                    ),
                    current_pos,
                )
            )
        if depth == max_count:
            return
        for matched_child, end in _match_node(
            node=node.node,
            text=text,
            pos=current_pos,
            grammar=grammar,
            memo=memo,
        ):
            if end == current_pos:
                return
            children.append(matched_child)
            dfs(end, depth + 1, children)
            children.pop()

    dfs(pos, 0, [])
    return results


_TEXT_SHAPE_COVERAGE_ITEMS: tuple[str, ...] = (
    "textshape:len:0",
    "textshape:len:1",
    "textshape:len:2-3",
    "textshape:len:4-7",
    "textshape:len:8-15",
    "textshape:len:16+",
    "textshape:content:empty",
    "textshape:content:nonempty",
    "textshape:content:digit",
    "textshape:content:alpha",
    "textshape:content:lower",
    "textshape:content:upper",
    "textshape:content:space",
    "textshape:content:punct",
)


def _coverage_site_label(base: str, segment: str) -> str:
    if not base:
        return segment
    return f"{base}_{segment}"


def _coverage_site_field(site_label: str) -> str:
    return site_label or "body"


def _repeat_count_bucket(count: int) -> str:
    if count >= 3:
        return "3+"
    return str(max(0, count))


def _repeat_bucket_values(node: Repeat) -> tuple[str, ...]:
    low = min(node.min_r, node.max_r)
    high = max(node.min_r, node.max_r)
    buckets: list[str] = []
    if low <= 0 <= high:
        buckets.append("0")
    if low <= 1 <= high:
        buckets.append("1")
    if low <= 2 <= high:
        buckets.append("2")
    if high >= 3:
        buckets.append("3+")
    return tuple(buckets)


def _depth_bucket(depth: int) -> str:
    if depth >= 2:
        return "2+"
    return str(max(0, depth))


def _text_length_bucket(length: int) -> str:
    if length <= 0:
        return "0"
    if length == 1:
        return "1"
    if length <= 3:
        return "2-3"
    if length <= 7:
        return "4-7"
    if length <= 15:
        return "8-15"
    return "16+"


def _text_shape_items(text: str) -> set[str]:
    items = {
        f"textshape:len:{_text_length_bucket(len(text))}",
        "textshape:content:nonempty" if text else "textshape:content:empty",
    }
    if any(char.isdigit() for char in text):
        items.add("textshape:content:digit")
    if any(char.isalpha() for char in text):
        items.add("textshape:content:alpha")
    if any(char.islower() for char in text):
        items.add("textshape:content:lower")
    if any(char.isupper() for char in text):
        items.add("textshape:content:upper")
    if any(char.isspace() for char in text):
        items.add("textshape:content:space")
    if any(not char.isalnum() and not char.isspace() for char in text):
        items.add("textshape:content:punct")
    return items


def _charclass_categories(chars: SequenceCollection[str]) -> tuple[str, ...]:
    categories: list[str] = []
    joined = "".join(chars)
    if any(char.isdigit() for char in joined):
        categories.append("digit")
    if any(char.isalpha() for char in joined):
        categories.append("alpha")
    if any(char.islower() for char in joined):
        categories.append("lower")
    if any(char.isupper() for char in joined):
        categories.append("upper")
    if any(char.isspace() for char in joined):
        categories.append("space")
    if any(not char.isalnum() and not char.isspace() for char in joined):
        categories.append("punct")
    return tuple(categories)


def _available_number_buckets(node: NumberRange) -> tuple[str, ...]:
    if not node.is_int:
        return ()
    low = int(min(node.min_val, node.max_val))
    high = int(max(node.min_val, node.max_val))
    buckets: list[str] = ["min", "max"]
    if low <= 0 <= high:
        buckets.append("zero")
    if low < 0:
        buckets.append("negative")
    if high > 0:
        buckets.append("positive")
    return tuple(dict.fromkeys(buckets))


def _number_boundary_items(
    *,
    owner_rule: str,
    owner_variant: str,
    site_label: str,
    text: str,
    node: NumberRange | None,
) -> set[str]:
    if node is None or not node.is_int:
        return set()
    try:
        value = int(text)
    except ValueError:
        return set()
    low = int(min(node.min_val, node.max_val))
    high = int(max(node.min_val, node.max_val))
    item_prefix = (
        f"number:{owner_rule}:{owner_variant}:{_coverage_site_field(site_label)}"
    )
    items: set[str] = set()
    if value == low:
        items.add(f"{item_prefix}:min")
    if value == high:
        items.add(f"{item_prefix}:max")
    if value == 0:
        items.add(f"{item_prefix}:zero")
    if value < 0:
        items.add(f"{item_prefix}:negative")
    if value > 0:
        items.add(f"{item_prefix}:positive")
    return items


def _append_ordered_coverage_item(
    ordered_items: list[str],
    seen_items: set[str],
    item: str,
) -> None:
    if item in seen_items:
        return
    seen_items.add(item)
    ordered_items.append(item)


def _rule_variant_nodes(rule_node: Node) -> list[tuple[str, Node]]:
    if isinstance(rule_node, Alternation):
        return [(str(index), option) for index, option in enumerate(rule_node.options)]
    return [("body", rule_node)]


def _collect_available_items_from_rule_node(
    *,
    owner_rule: str,
    owner_variant: str,
    node: Node,
    grammar: Grammar,
    ordered_items: list[str],
    seen_items: set[str],
    site_label: str = "",
) -> None:
    site_field = _coverage_site_field(site_label)
    if isinstance(node, Ref):
        child_rule_name = _normalize_rule_name(node.name)
        child_rule = grammar.rules.get(child_rule_name)
        if isinstance(child_rule, Alternation):
            for option_index in range(len(child_rule.options)):
                _append_ordered_coverage_item(
                    ordered_items,
                    seen_items,
                    (
                        f"site:{owner_rule}:{owner_variant}:{site_field}:"
                        f"{child_rule_name}:{option_index}"
                    ),
                )
        return
    if isinstance(node, Sequence):
        for index, child in enumerate(node.nodes):
            _collect_available_items_from_rule_node(
                owner_rule=owner_rule,
                owner_variant=owner_variant,
                node=child,
                grammar=grammar,
                ordered_items=ordered_items,
                seen_items=seen_items,
                site_label=_coverage_site_label(site_label, f"slot{index}"),
            )
        return
    if isinstance(node, Repeat):
        for bucket in _repeat_bucket_values(node):
            _append_ordered_coverage_item(
                ordered_items,
                seen_items,
                f"repeat:{owner_rule}:{owner_variant}:{site_field}:{bucket}",
            )
        _collect_available_items_from_rule_node(
            owner_rule=owner_rule,
            owner_variant=owner_variant,
            node=node.node,
            grammar=grammar,
            ordered_items=ordered_items,
            seen_items=seen_items,
            site_label=_coverage_site_label(site_label, "item"),
        )
        return
    if isinstance(node, Alternation):
        for option in node.options:
            _collect_available_items_from_rule_node(
                owner_rule=owner_rule,
                owner_variant=owner_variant,
                node=option,
                grammar=grammar,
                ordered_items=ordered_items,
                seen_items=seen_items,
                site_label=site_label,
            )
        return
    if isinstance(node, CharClass):
        for category in _charclass_categories(node.chars):
            _append_ordered_coverage_item(
                ordered_items,
                seen_items,
                f"charclass:{owner_rule}:{owner_variant}:{site_field}:{category}",
            )
        return
    if isinstance(node, NumberRange):
        for bucket in _available_number_buckets(node):
            _append_ordered_coverage_item(
                ordered_items,
                seen_items,
                f"number:{owner_rule}:{owner_variant}:{site_field}:{bucket}",
            )
        return


def _parse_tree_variant_label(tree: ParseTreeNode, rule_node: Node) -> str:
    if isinstance(rule_node, Alternation) and tree.choice_index is not None:
        return str(tree.choice_index)
    return "body"


def _collect_dynamic_items_from_rule_tree(
    *,
    tree: ParseTreeNode,
    rule_name: str,
    grammar: Grammar,
    items: set[str],
    active_rule_counts: dict[str, int],
) -> None:
    normalized_rule_name = _normalize_rule_name(rule_name)
    rule_node = grammar.rules.get(normalized_rule_name)
    if rule_node is None:
        return

    owner_variant = _parse_tree_variant_label(tree, rule_node)
    if isinstance(rule_node, Alternation) and tree.choice_index is not None:
        items.add(f"production:{normalized_rule_name}:{tree.choice_index}")

    next_counts = dict(active_rule_counts)
    current_depth = next_counts.get(normalized_rule_name, 0)
    if normalized_rule_name in grammar.recursive_rule_names:
        items.add(f"depth:{normalized_rule_name}:{_depth_bucket(current_depth)}")
        next_counts[normalized_rule_name] = current_depth + 1

    def _visit(node: ParseTreeNode, site_label: str) -> None:
        site_field = _coverage_site_field(site_label)
        if node.kind == "ref":
            child_rule_name = (
                _normalize_rule_name(node.ref_name) if node.ref_name is not None else ""
            )
            child = node.children[0] if node.children else None
            child_rule = grammar.rules.get(child_rule_name)
            if (
                child is not None
                and child_rule_name
                and isinstance(child_rule, Alternation)
                and child.choice_index is not None
            ):
                items.add(
                    (
                        f"site:{normalized_rule_name}:{owner_variant}:{site_field}:"
                        f"{child_rule_name}:{child.choice_index}"
                    )
                )
            if child is not None and child_rule_name:
                _collect_dynamic_items_from_rule_tree(
                    tree=child,
                    rule_name=child_rule_name,
                    grammar=grammar,
                    items=items,
                    active_rule_counts=next_counts,
                )
            return
        if node.kind == "sequence":
            for index, child in enumerate(node.children):
                _visit(child, _coverage_site_label(site_label, f"slot{index}"))
            return
        if node.kind == "repeat":
            items.add(
                f"repeat:{normalized_rule_name}:{owner_variant}:{site_field}:{_repeat_count_bucket(len(node.children))}"
            )
            for child in node.children:
                _visit(child, _coverage_site_label(site_label, "item"))
            return
        if node.kind == "alternation":
            if node.children:
                _visit(node.children[0], site_label)
            return
        if node.kind == "charclass":
            for category in _charclass_categories((node.text,)):
                items.add(
                    f"charclass:{normalized_rule_name}:{owner_variant}:{site_field}:{category}"
                )
            return
        if node.kind == "numberrange":
            items.update(
                _number_boundary_items(
                    owner_rule=normalized_rule_name,
                    owner_variant=owner_variant,
                    site_label=site_label,
                    text=node.text,
                    node=node.grammar_node if isinstance(node.grammar_node, NumberRange) else None,
                )
            )
            return
        for child in node.children:
            _visit(child, site_label)

    _visit(tree, "")


def _normalize_coverage_item_name(name: str) -> str:
    if name.startswith("rule:"):
        return f"rule:{_normalize_rule_name(name[5:])}"
    if name.startswith("production:"):
        parts = name.split(":", 2)
        if len(parts) == 3 and parts[1]:
            return f"production:{_normalize_rule_name(parts[1])}:{parts[2]}"
    if name.startswith("site:"):
        parts = name.split(":")
        if len(parts) == 6 and parts[1] and parts[4]:
            return (
                f"site:{_normalize_rule_name(parts[1])}:{parts[2]}:{parts[3]}:"
                f"{_normalize_rule_name(parts[4])}:{parts[5]}"
            )
    if name.startswith("depth:"):
        parts = name.split(":", 2)
        if len(parts) == 3 and parts[1]:
            return f"depth:{_normalize_rule_name(parts[1])}:{parts[2]}"
    if name.startswith("repeat:"):
        parts = name.split(":", 4)
        if len(parts) == 5 and parts[1]:
            return f"repeat:{_normalize_rule_name(parts[1])}:{parts[2]}:{parts[3]}:{parts[4]}"
    if name.startswith("charclass:"):
        parts = name.split(":", 4)
        if len(parts) == 5 and parts[1]:
            return (
                f"charclass:{_normalize_rule_name(parts[1])}:"
                f"{parts[2]}:{parts[3]}:{parts[4]}"
            )
    if name.startswith("number:"):
        parts = name.split(":", 4)
        if len(parts) == 5 and parts[1]:
            return f"number:{_normalize_rule_name(parts[1])}:{parts[2]}:{parts[3]}:{parts[4]}"
    return name


def _coverage_item_rule_name(item: str) -> str | None:
    normalized = _normalize_coverage_item_name(item)
    if normalized.startswith("rule:"):
        return normalized[5:]
    if normalized.startswith("production:"):
        parts = normalized.split(":", 2)
        if len(parts) == 3 and parts[1]:
            return parts[1]
    if normalized.startswith("site:"):
        parts = normalized.split(":")
        if len(parts) == 6 and parts[4]:
            return parts[4]
    if normalized.startswith("depth:"):
        parts = normalized.split(":", 2)
        if len(parts) == 3 and parts[1]:
            return parts[1]
    if normalized.startswith("repeat:"):
        parts = normalized.split(":", 4)
        if len(parts) == 5 and parts[1]:
            return parts[1]
    if normalized.startswith("charclass:"):
        parts = normalized.split(":", 4)
        if len(parts) == 5 and parts[1]:
            return parts[1]
    if normalized.startswith("number:"):
        parts = normalized.split(":", 4)
        if len(parts) == 5 and parts[1]:
            return parts[1]
    return None


def _reachable_rule_names_from_node(
    node: Node,
    *,
    grammar: Grammar,
    active_rule_names: set[str] | None = None,
) -> set[str]:
    if isinstance(node, Ref):
        names = {node.name}
        if node.name not in grammar.rules:
            return names
        if active_rule_names is not None and node.name in active_rule_names:
            return names
        next_active = set(active_rule_names or set())
        next_active.add(node.name)
        names.update(
            _reachable_rule_names_from_node(
                grammar.rules[node.name],
                grammar=grammar,
                active_rule_names=next_active,
            )
        )
        return names
    if isinstance(node, Sequence):
        names: set[str] = set()
        for child in node.nodes:
            names.update(
                _reachable_rule_names_from_node(
                    child,
                    grammar=grammar,
                    active_rule_names=active_rule_names,
                )
            )
        return names
    if isinstance(node, Alternation):
        names: set[str] = set()
        for option in node.options:
            names.update(
                _reachable_rule_names_from_node(
                    option,
                    grammar=grammar,
                    active_rule_names=active_rule_names,
                )
            )
        return names
    if isinstance(node, Repeat):
        return _reachable_rule_names_from_node(
            node.node,
            grammar=grammar,
            active_rule_names=active_rule_names,
        )
    return set()


def _node_matches_preferred_coverage(
    *,
    node: Node,
    grammar: Grammar,
    preferred_coverage_items: set[str],
) -> bool:
    preferred_rule_names = {
        rule_name
        for item in preferred_coverage_items
        for rule_name in [_coverage_item_rule_name(item)]
        if rule_name is not None
    }
    if not preferred_rule_names:
        return False
    reachable = _reachable_rule_names_from_node(node, grammar=grammar)
    return bool(reachable & preferred_rule_names)


def _alternation_preference_score(
    *,
    option: Node,
    option_index: int,
    current_rule_name: str | None,
    grammar: Grammar,
    preferred_coverage_items: set[str],
) -> int:
    score = 0
    if current_rule_name is not None:
        production_item = f"production:{_normalize_rule_name(current_rule_name)}:{option_index}"
        if production_item in preferred_coverage_items:
            score += 1000

    preferred_rule_names = {
        rule_name
        for item in preferred_coverage_items
        for rule_name in [_coverage_item_rule_name(item)]
        if rule_name is not None
    }
    if not preferred_rule_names:
        preferred_rule_names = set()

    reachable = _reachable_rule_names_from_node(option, grammar=grammar)
    if current_rule_name is not None:
        normalized_current_rule = _normalize_rule_name(current_rule_name)
        for item in preferred_coverage_items:
            if not item.startswith("site:"):
                continue
            parts = item.split(":")
            if len(parts) != 6:
                continue
            parent_rule, parent_variant, _slot, child_rule, child_variant = parts[1:]
            if normalized_current_rule == parent_rule and str(option_index) == parent_variant:
                score += 600
                if child_rule in reachable:
                    score += 200
            if normalized_current_rule == child_rule and str(option_index) == child_variant:
                score += 900
    if current_rule_name is not None and current_rule_name in preferred_rule_names:
        score += 100
    score += len(reachable & preferred_rule_names) * 10
    return score


def _referenced_rule_names(node: Node) -> set[str]:
    """Return all rule names reachable by Ref nodes inside one grammar AST."""
    if isinstance(node, Ref):
        return {node.name}
    if isinstance(node, Sequence):
        names: set[str] = set()
        for child in node.nodes:
            names.update(_referenced_rule_names(child))
        return names
    if isinstance(node, Alternation):
        names: set[str] = set()
        for option in node.options:
            names.update(_referenced_rule_names(option))
        return names
    if isinstance(node, Repeat):
        return _referenced_rule_names(node.node)
    return set()


def _rule_complexity(node: Node) -> int:
    """Estimate how structurally rich a rule is for tie-breaking among matches."""
    if isinstance(node, (Literal, CharClass, NumberRange, Ref)):
        return 1
    if isinstance(node, Sequence):
        return 1 + sum(_rule_complexity(child) for child in node.nodes)
    if isinstance(node, Alternation):
        return 1 + sum(_rule_complexity(option) for option in node.options)
    if isinstance(node, Repeat):
        return 1 + _rule_complexity(node.node)
    return 1


def _start_rule_priority(
    *,
    candidate: str,
    requested: str,
    inbound_ref_counts: dict[str, int],
    grammar: Grammar,
) -> tuple[int, int, int, str]:
    """Rank fallback start-rule candidates using grammar-graph heuristics.

    Lower tuples are tried first. The ordering prefers:
    1. the requested rule itself
    2. obvious requested-rule aliases like foo <-> foo_start
    3. explicit *_start top-level rules
    4. rules not referenced by other rules
    5. structurally richer rules over tiny helper rules
    """
    if candidate == requested:
        family_rank = 0
    elif candidate == f"{requested}_start" or (
        requested.endswith("_start") and candidate == requested[:-6]
    ):
        family_rank = 1
    elif candidate.endswith("_start"):
        family_rank = 2
    elif inbound_ref_counts.get(candidate, 0) == 0:
        family_rank = 3
    else:
        family_rank = 4
    inbound_rank = inbound_ref_counts.get(candidate, 0)
    complexity_rank = -_rule_complexity(grammar.rules[candidate])
    return (family_rank, inbound_rank, complexity_rank, candidate)


def _candidate_start_rules(grammar: Grammar, start_rule: str) -> list[str]:
    """Infer likely top-level fallback rules directly from the grammar graph.

    This avoids hardcoding JSON/IP-specific rule sets. New grammar files can
    participate automatically as long as they define sensible top-level rules
    such as ``foo_start`` or rules that are not only referenced as helpers.
    """
    inbound_ref_counts: dict[str, int] = {name: 0 for name in grammar.rules}
    for node in grammar.rules.values():
        for ref_name in _referenced_rule_names(node):
            if ref_name in inbound_ref_counts:
                inbound_ref_counts[ref_name] += 1

    candidates = sorted(
        grammar.rules,
        key=lambda candidate: _start_rule_priority(
            candidate=candidate,
            requested=start_rule,
            inbound_ref_counts=inbound_ref_counts,
            grammar=grammar,
        ),
    )
    return candidates


def parse_from_rule(
    *,
    text: str,
    start_rule: str,
    mutator_kind: str = "grammar",
) -> ParseTreeNode | None:
    """Parse seed text into a derivation tree under one explicit grammar rule.

    This is the generic path for new formats supplied via external grammar
    files: if a caller can name the right start rule, the mutator can attempt
    exact tree recovery from the seed before mutating it.
    """
    normalized_start_rule = _normalize_rule_name(start_rule)
    grammar = _resolved_base_grammar(mutator_kind=mutator_kind)
    if normalized_start_rule not in grammar.rules:
        raise KeyError(f"unknown grammar start rule {normalized_start_rule!r}")
    matched = _match_exact_node(
        node=grammar.rules[normalized_start_rule],
        text=text,
        grammar=grammar,
    )
    if matched is not None:
        return matched
    for candidate_rule in _candidate_start_rules(grammar, normalized_start_rule):
        if candidate_rule == normalized_start_rule:
            continue
        matched = _match_exact_node(
            node=grammar.rules[candidate_rule],
            text=text,
            grammar=grammar,
        )
        if matched is not None:
            return ParseTreeNode(
                kind="ref",
                text=text,
                children=[matched],
                ref_name=candidate_rule,
                grammar_node=Ref(candidate_rule),
            )
    return None


def _default_start_rule(grammar: Grammar) -> str:
    return grammar.start_rule or next(iter(grammar.rules))


def _collect_reachable_rule_names(
    *,
    grammar: Grammar,
    start_rule: str,
) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    queue = [_normalize_rule_name(start_rule)]
    while queue:
        rule_name = queue.pop(0)
        if rule_name in seen or rule_name not in grammar.rules:
            continue
        seen.add(rule_name)
        ordered.append(rule_name)
        for child_name in sorted(_collect_direct_ref_names(grammar.rules[rule_name])):
            if child_name not in seen:
                queue.append(child_name)
    return ordered


def _collect_coverage_items_from_parse_tree(
    *,
    tree: ParseTreeNode,
    root_rule_name: str,
    grammar: Grammar,
) -> set[str]:
    items = set(_text_shape_items(tree.to_text()))
    _collect_dynamic_items_from_rule_tree(
        tree=tree,
        rule_name=root_rule_name,
        grammar=grammar,
        items=items,
        active_rule_counts={},
    )
    return items


def _parse_tree_root_wrapper_depth(node: ParseTreeNode) -> int:
    depth = 0
    current = node
    while current.kind in {"ref", "alternation"} and len(current.children) == 1:
        depth += 1
        current = current.children[0]
    return depth


def _parse_tree_text_span(node: ParseTreeNode) -> int:
    return len(node.text)


def _exact_parse_profile(
    *,
    text: str,
    grammar: Grammar,
    requested_start_rule: str,
) -> tuple[str, ParseTreeNode] | None:
    normalized_start_rule = _normalize_rule_name(requested_start_rule)
    candidate_rules = _candidate_start_rules(grammar, normalized_start_rule)
    best_match: tuple[tuple[int, int, int, int, str], str, ParseTreeNode] | None = None
    for order, candidate_rule in enumerate(candidate_rules):
        matched = _match_exact_node(
            node=grammar.rules[candidate_rule],
            text=text,
            grammar=grammar,
        )
        if matched is None:
            continue
        score = (
            _parse_tree_root_wrapper_depth(matched),
            -_rule_complexity(grammar.rules[candidate_rule]),
            -_parse_tree_text_span(matched),
            order,
            candidate_rule,
        )
        if best_match is None or score < best_match[0]:
            best_match = (score, candidate_rule, matched)
    if best_match is None:
        return None
    return (best_match[1], best_match[2])


def _partial_parse_start_positions(
    text: str,
    *,
    limit: int = _MAX_PARTIAL_PARSE_POSITIONS,
) -> list[int]:
    if not text:
        return []
    if len(text) <= 64:
        return list(range(len(text)))

    ordered: list[int] = []
    seen: set[int] = set()

    def _record(position: int) -> None:
        if position < 0 or position >= len(text) or position in seen:
            return
        seen.add(position)
        ordered.append(position)

    _record(0)
    for position in range(1, min(len(text), 24)):
        _record(position)
    for position in range(max(1, len(text) - 24), len(text)):
        _record(position)

    for position in range(1, len(text)):
        prev_char = text[position - 1]
        current_char = text[position]
        if (
            prev_char.isspace() != current_char.isspace()
            or prev_char.isalnum() != current_char.isalnum()
            or not prev_char.isalnum()
            or not current_char.isalnum()
        ):
            _record(position)

    if len(ordered) <= limit:
        return ordered

    sampled: list[int] = []
    sampled_seen: set[int] = set()

    def _record_sample(position: int) -> None:
        if position in sampled_seen or position < 0 or position >= len(text):
            return
        sampled_seen.add(position)
        sampled.append(position)

    stride = max(1, len(ordered) // limit)
    for position in ordered[::stride]:
        _record_sample(position)
        if len(sampled) >= max(1, limit - 1):
            break
    _record_sample(len(text) - 1)
    return sampled


def _partial_parse_profile(
    *,
    text: str,
    grammar: Grammar,
    requested_start_rule: str,
) -> PartialParseMatch | None:
    if not text:
        return None

    normalized_start_rule = _normalize_rule_name(requested_start_rule)
    candidate_rules = _candidate_start_rules(grammar, normalized_start_rule)
    candidate_positions = _partial_parse_start_positions(text)
    best_match: tuple[tuple[int, int, int, int, int, int], PartialParseMatch] | None = None

    for order, candidate_rule in enumerate(candidate_rules):
        rule_node = grammar.rules[candidate_rule]
        memo: dict[tuple[int, int], list[tuple[ParseTreeNode, int]]] = {}
        complexity = _rule_complexity(rule_node)
        for start in candidate_positions:
            for matched, end in _match_node(
                node=rule_node,
                text=text,
                pos=start,
                grammar=grammar,
                memo=memo,
            ):
                if end <= start:
                    continue
                span = end - start
                edge_touches = int(start == 0) + int(end == len(text))
                score = (
                    span,
                    edge_touches,
                    -_parse_tree_root_wrapper_depth(matched),
                    complexity,
                    -order,
                    -start,
                )
                partial_match = PartialParseMatch(
                    rule_name=candidate_rule,
                    tree=matched,
                    start=start,
                    end=end,
                )
                if best_match is None or score > best_match[0]:
                    best_match = (score, partial_match)

    if best_match is None:
        return None
    return best_match[1]


def available_coverage_items(*, mutator_kind: str = "grammar") -> list[str]:
    """Return ordered grammar coverage items reachable from the default start rule."""
    grammar = _resolved_base_grammar(mutator_kind=mutator_kind)
    start_rule = _default_start_rule(grammar)
    ordered_items: list[str] = []
    seen_items: set[str] = set()
    for rule_name in _collect_reachable_rule_names(grammar=grammar, start_rule=start_rule):
        rule_node = grammar.rules.get(rule_name)
        if rule_node is None:
            continue
        if rule_name in grammar.recursive_rule_names:
            for bucket in ("0", "1", "2+"):
                _append_ordered_coverage_item(
                    ordered_items,
                    seen_items,
                    f"depth:{rule_name}:{bucket}",
                )
        for variant_label, variant_node in _rule_variant_nodes(rule_node):
            if variant_label != "body":
                _append_ordered_coverage_item(
                    ordered_items,
                    seen_items,
                    f"production:{rule_name}:{variant_label}",
                )
            _collect_available_items_from_rule_node(
                owner_rule=rule_name,
                owner_variant=variant_label,
                node=variant_node,
                grammar=grammar,
                ordered_items=ordered_items,
                seen_items=seen_items,
            )
    for item in _TEXT_SHAPE_COVERAGE_ITEMS:
        _append_ordered_coverage_item(ordered_items, seen_items, item)
    return ordered_items


def coverage_items_for_text(
    *,
    text: str,
    mutator_kind: str = "grammar",
) -> set[str]:
    """Return covered grammar productions and richer structural buckets for one input."""
    if not text:
        return set()
    grammar = _resolved_base_grammar(mutator_kind=mutator_kind)
    start_rule = _default_start_rule(grammar)
    matched_start_tree = _match_exact_node(
        node=grammar.rules[start_rule],
        text=text,
        grammar=grammar,
    )
    if matched_start_tree is not None:
        return _collect_coverage_items_from_parse_tree(
            tree=matched_start_tree,
            root_rule_name=start_rule,
            grammar=grammar,
        )
    parse_profile = _exact_parse_profile(
        text=text,
        grammar=grammar,
        requested_start_rule=start_rule,
    )
    if parse_profile is None:
        partial_match = _partial_parse_profile(
            text=text,
            grammar=grammar,
            requested_start_rule=start_rule,
        )
        if partial_match is None:
            return set()
        partial_items = _collect_coverage_items_from_parse_tree(
            tree=partial_match.tree,
            root_rule_name=partial_match.rule_name,
            grammar=grammar,
        )
        partial_items.update(_text_shape_items(text))
        return partial_items
    matched_rule_name, tree = parse_profile
    return _collect_coverage_items_from_parse_tree(
        tree=tree,
        root_rule_name=matched_rule_name,
        grammar=grammar,
    )


def _build_from_ast_spec(*, mutator_kind: str) -> Grammar:
    ast_grammar_spec = resolve_ast_grammar_spec(kind=mutator_kind)
    recursive_symbols = ast_grammar_spec.get("recursive_symbols", set())
    grammar = Grammar(
        start_rule=str(ast_grammar_spec["start"]),
        recursive_rule_names=set(recursive_symbols)
        if isinstance(recursive_symbols, set)
        else set(recursive_symbols),
    )
    rules = ast_grammar_spec.get("rules")
    if not isinstance(rules, dict):
        raise TypeError("ast grammar spec rules must be a dictionary")
    for name, expr in rules.items():
        if not isinstance(name, str) or not isinstance(expr, str):
            raise TypeError("ast grammar spec rules must map strings to strings")
        grammar.add(name, expr)
    return grammar


def build(*, mutator_kind: str = "grammar") -> Grammar:
    """Build the AST grammar used by grammar_ast for one mutator kind."""
    return _build_from_ast_spec(mutator_kind=mutator_kind)


def configure(*, grammar_rules_file: str | None) -> None:
    """Configure an optional external rules file and clear the grammar cache."""
    global _GRAMMAR_RULES_FILE
    _GRAMMAR_RULES_FILE = grammar_rules_file
    _base_grammar.cache_clear()


def _parse_rule_definition(line: str) -> tuple[str, str]:
    """Parse one external rule definition line into (name, expression)."""
    if "::=" in line:
        name, expr = line.split("::=", 1)
    elif ":=" in line:
        name, expr = line.split(":=", 1)
    elif "=" in line:
        name, expr = line.split("=", 1)
    else:
        raise ValueError(f"invalid grammar rule line: {line!r}")
    return name.strip(), expr.strip()


def _apply_extra_rules(grammar: Grammar, *, grammar_rules_file: str | None) -> None:
    """Load additional DSL rules from the configured rules file."""
    if not grammar_rules_file:
        return
    path = Path(grammar_rules_file)
    with path.open(encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            name, expr = _parse_rule_definition(line)
            grammar.add(name, expr)


@lru_cache(maxsize=16)
def _base_grammar(
    *,
    mutator_kind: str,
    grammar_rules_file: str | None,
    grammar_version: int,
) -> Grammar:
    """Return the cached base grammar with any external rules applied."""
    del grammar_version
    grammar = build(mutator_kind=mutator_kind)
    _apply_extra_rules(grammar, grammar_rules_file=grammar_rules_file)
    return grammar


def _resolved_base_grammar(*, mutator_kind: str) -> Grammar:
    return _base_grammar(
        mutator_kind=mutator_kind,
        grammar_rules_file=_GRAMMAR_RULES_FILE,
        grammar_version=runtime_grammar_version(),
    )


def _expand_preferred_rules(grammar: Grammar, names: list[str]) -> list[str]:
    """Expand preferred rule names to include existing related overlay names."""
    ordered: list[str] = []
    seen: set[str] = set()
    for name in names:
        normalized_name = _normalize_rule_name(name)
        for candidate in (normalized_name, f"{normalized_name}_mut"):
            if candidate in grammar.rules and candidate not in seen:
                ordered.append(candidate)
                seen.add(candidate)
    return ordered


def _mutate_parse_tree(
    tree: ParseTreeNode,
    rng: random.Random,
    grammar: Grammar,
) -> ParseTreeNode:
    """Pick one parsed node and apply its generic node-type mutation operator."""
    mutable_nodes = tree.walk_mutable_nodes()
    chosen = rng.choice(mutable_nodes)
    if chosen is tree:
        return tree.mutate_self(rng, grammar)

    def _replace(node: ParseTreeNode) -> ParseTreeNode:
        if node is chosen:
            return node.mutate_self(rng, grammar)
        node.children = [_replace(child) for child in node.children]
        node.text = node.to_text()
        return node

    return _replace(tree)


def _mutate_partial_parse_match(
    *,
    original_text: str,
    partial_match: PartialParseMatch,
    rng: random.Random,
    grammar: Grammar,
    min_mutation_rounds: int,
    max_mutation_rounds: int,
) -> str | None:
    last_candidate: str | None = None
    for _attempt in range(8):
        tree = copy.deepcopy(partial_match.tree)
        for _ in range(rng.randint(min_mutation_rounds, max_mutation_rounds)):
            tree = _mutate_parse_tree(tree, rng, grammar)
        candidate = (
            original_text[: partial_match.start]
            + tree.to_text()
            + original_text[partial_match.end :]
        )
        last_candidate = candidate
        if candidate != original_text:
            return candidate

    if partial_match.rule_name in grammar.rules:
        replacement = _generate_parse_subtree_from_node(
            grammar.rules[partial_match.rule_name],
            grammar,
            rng,
        ).to_text()
        candidate = (
            original_text[: partial_match.start]
            + replacement
            + original_text[partial_match.end :]
        )
        if candidate != original_text:
            return candidate
    return last_candidate if last_candidate != original_text else None


def _collect_direct_ref_names(node: Node) -> set[str]:
    if isinstance(node, Ref):
        return {node.name}
    if isinstance(node, Sequence):
        out: set[str] = set()
        for child in node.nodes:
            out.update(_collect_direct_ref_names(child))
        return out
    if isinstance(node, Alternation):
        out: set[str] = set()
        for option in node.options:
            out.update(_collect_direct_ref_names(option))
        return out
    if isinstance(node, Repeat):
        return _collect_direct_ref_names(node.node)
    return set()


def _preferred_rule_neighborhood(
    *,
    grammar: Grammar,
    start_rule: str,
    limit: int = 8,
) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    queue = [start_rule]
    while queue and len(ordered) < limit:
        name = queue.pop(0)
        if name not in grammar.rules or name in seen:
            continue
        ordered.append(name)
        seen.add(name)
        for child_name in sorted(_collect_direct_ref_names(grammar.rules[name])):
            if child_name not in seen:
                queue.append(child_name)
    return _expand_preferred_rules(grammar, ordered)


def _blend_with_seed(
    *,
    original_text: str,
    generated_text: str,
    rng: random.Random,
) -> str:
    """Blend a generated candidate back into the original seed text."""
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
    preferred_coverage_items: list[str] | None = None,
) -> list[str]:
    """Generate unique seedless candidates from the built grammar."""
    if count <= 0:
        return []
    base = _resolved_base_grammar(mutator_kind=mutator_kind)
    start_rule = _default_start_rule(base)
    preferred = _preferred_rule_neighborhood(grammar=base, start_rule=start_rule)
    outputs: list[str] = []
    seen: set[str] = set()
    attempts = 0
    max_attempts = max(count * 10, 25)
    while len(outputs) < count and attempts < max_attempts:
        attempts += 1
        grammar = copy.deepcopy(base)
        for _round in range(rng.randint(0, 3)):
            grammar.mutate(rng, preferred_rule_names=preferred)
        try:
            generated = grammar.generate(
                start_rule,
                rng,
                preferred_coverage_items=preferred_coverage_items,
            )
        except _GenerationError:
            continue
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
    preferred_coverage_items: list[str] | None = None,
    mutator_kind: str = "grammar",
) -> list[str]:
    """Generate unique candidates from a specific start rule."""
    if count <= 0:
        return []
    if min_mutation_rounds < 0 or max_mutation_rounds < min_mutation_rounds:
        raise ValueError("invalid mutation round bounds")
    normalized_start_rule = _normalize_rule_name(start_rule)
    base = _resolved_base_grammar(mutator_kind=mutator_kind)
    if normalized_start_rule not in base.rules:
        raise KeyError(f"unknown grammar start rule {normalized_start_rule!r}")
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
        try:
            generated = grammar.generate(
                normalized_start_rule,
                rng,
                preferred_coverage_items=preferred_coverage_items,
            )
        except _GenerationError:
            continue
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
    mutator_kind: str = "grammar",
) -> str:
    """Mutate from one explicit start rule.

    Prefer exact derivation-tree mutation when the seed can be parsed under the
    requested start rule. Fall back to grammar-AST mutation and generation when
    exact parsing is not available.
    """
    if min_mutation_rounds < 0 or max_mutation_rounds < min_mutation_rounds:
        raise ValueError("invalid mutation round bounds")
    normalized_start_rule = _normalize_rule_name(start_rule)
    base = _resolved_base_grammar(mutator_kind=mutator_kind)
    if normalized_start_rule not in base.rules:
        raise KeyError(f"unknown grammar start rule {normalized_start_rule!r}")
    if text:
        parsed_tree = parse_from_rule(
            text=text,
            start_rule=normalized_start_rule,
            mutator_kind=mutator_kind,
        )
        if parsed_tree is not None:
            for _attempt in range(8):
                tree = copy.deepcopy(parsed_tree)
                for _ in range(rng.randint(min_mutation_rounds, max_mutation_rounds)):
                    tree = _mutate_parse_tree(tree, rng, base)
                mutated_text = tree.to_text()
                if mutated_text != text:
                    return mutated_text
            return tree.to_text()
        partial_match = _partial_parse_profile(
            text=text,
            grammar=base,
            requested_start_rule=normalized_start_rule,
        )
        if partial_match is not None:
            salvaged = _mutate_partial_parse_match(
                original_text=text,
                partial_match=partial_match,
                rng=rng,
                grammar=base,
                min_mutation_rounds=min_mutation_rounds,
                max_mutation_rounds=max_mutation_rounds,
            )
            if salvaged is not None:
                return salvaged
    preferred = _expand_preferred_rules(base, preferred_rule_names or [])
    grammar = copy.deepcopy(base)
    for _ in range(rng.randint(min_mutation_rounds, max_mutation_rounds)):
        grammar.mutate(rng, preferred_rule_names=preferred)
    try:
        generated = grammar.generate(normalized_start_rule, rng)
    except _GenerationError:
        generated = base.generate(normalized_start_rule, rng)
    if blend_with_seed and text:
        return _blend_with_seed(original_text=text, generated_text=generated, rng=rng)
    return generated


def mutate(
    text: str,
    *,
    mutator_kind: str,
    rng: random.Random,
) -> str:
    """Main mutator entry point.

    Prefer exact derivation-tree mutation under the inferred grammar start rule.
    If exact parsing fails, fall back to grammar-AST mutation and generation.
    """
    base = _resolved_base_grammar(mutator_kind=mutator_kind)
    start_rule = _default_start_rule(base)
    preferred = _preferred_rule_neighborhood(
        grammar=base,
        start_rule=start_rule,
    )
    parse_profile = (
        _exact_parse_profile(
            text=text,
            grammar=base,
            requested_start_rule=start_rule,
        )
        if text
        else None
    )
    if parse_profile is not None:
        start_rule, parsed_tree = parse_profile
        preferred = _preferred_rule_neighborhood(
            grammar=base,
            start_rule=start_rule,
        )
        for _attempt in range(8):
            tree = copy.deepcopy(parsed_tree)
            mutation_rounds = rng.randint(1, 5)
            for _ in range(mutation_rounds):
                tree = _mutate_parse_tree(tree, rng, base)
                mutated_text = tree.to_text()
            if mutated_text != text:
                return mutated_text
        return tree.to_text()
    partial_match = (
        _partial_parse_profile(
            text=text,
            grammar=base,
            requested_start_rule=start_rule,
        )
        if text
        else None
    )
    if partial_match is not None:
        salvaged = _mutate_partial_parse_match(
            original_text=text,
            partial_match=partial_match,
            rng=rng,
            grammar=base,
            min_mutation_rounds=1,
            max_mutation_rounds=5,
        )
        if salvaged is not None:
            return salvaged
    grammar = copy.deepcopy(base)
    mutation_rounds = rng.randint(1, 5)
    for _ in range(mutation_rounds):
        grammar.mutate(rng, preferred_rule_names=preferred)
    try:
        generated = grammar.generate(start_rule, rng)
    except _GenerationError:
        generated = base.generate(start_rule, rng)
    if text:
        return _blend_with_seed(original_text=text, generated_text=generated, rng=rng)
    return generated


__all__ = [
    "Grammar",
    "Node",
    "ParseTreeNode",
    "Literal",
    "CharClass",
    "Sequence",
    "Alternation",
    "Repeat",
    "NumberRange",
    "Ref",
    "build",
    "configure",
    "coverage_items_for_text",
    "generate_from_rule",
    "generate_without_seed",
    "mutate",
    "mutate_from_rule",
    "available_coverage_items",
    "parse",
    "parse_from_rule",
    "tokenize",
]
