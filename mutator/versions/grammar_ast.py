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
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

_PRINTABLE_ASCII = [chr(code) for code in range(32, 127)]
_GRAMMAR_RULES_FILE: str | None = None


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

    def to_json_value(self) -> object:
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

    def to_json_value(self) -> object:
        """Best-effort JSON conversion for compatibility with SeedTreeNode."""
        return self.to_text()


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
    """Container for named grammar rules plus generation/mutation helpers."""
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
    if isinstance(grammar_node, NumberRange):
        if rng.random() < 0.7:
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
        generated = node.generate(grammar.rules, rng)
        parsed = _match_exact_node(node=node, text=generated, grammar=grammar)
        if parsed is not None:
            return parsed
    return ParseTreeNode(kind="literal", text=node.generate(grammar.rules, rng), grammar_node=node)


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
    action = rng.choice(["add", "drop", "duplicate", "mutate_child"])
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
        for option in node.options:
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


def parse_from_rule(*, text: str, start_rule: str) -> ParseTreeNode | None:
    """Parse seed text into a derivation tree under one explicit grammar rule.

    This is the generic path for new formats supplied via external grammar
    files: if a caller can name the right start rule, the mutator can attempt
    exact tree recovery from the seed before mutating it.
    """
    grammar = _base_grammar()
    if start_rule not in grammar.rules:
        raise KeyError(f"unknown grammar start rule {start_rule!r}")
    for candidate_rule in _candidate_start_rules(grammar, start_rule):
        matched = _match_exact_node(node=grammar.rules[candidate_rule], text=text, grammar=grammar)
        if matched is not None:
            if candidate_rule == start_rule:
                return matched
            return ParseTreeNode(
                kind="ref",
                text=text,
                children=[matched],
                ref_name=candidate_rule,
                grammar_node=Ref(candidate_rule),
            )
    return None


@dataclass
class JsonStringNode(SeedTreeNode):
    value: str

    def mutate_self(self, rng: random.Random, grammar: Grammar) -> SeedTreeNode:
        literal = Literal(self.value)
        literal.mutate(grammar.rules, rng)
        self.value = literal.value
        return self

    def to_text(self) -> str:
        return json.dumps(self.value, separators=(",", ":"))

    def to_json_value(self) -> object:
        return self.value


@dataclass
class JsonNumberNode(SeedTreeNode):
    value: int | float

    def mutate_self(self, rng: random.Random, grammar: Grammar) -> SeedTreeNode:
        if isinstance(self.value, int) and not isinstance(self.value, bool):
            self.value += rng.randint(-10, 10)
        else:
            self.value += rng.uniform(-5.0, 5.0)
        return self

    def to_text(self) -> str:
        return json.dumps(self.value, separators=(",", ":"))

    def to_json_value(self) -> object:
        return self.value


@dataclass
class JsonBoolNode(SeedTreeNode):
    value: bool

    def mutate_self(self, rng: random.Random, grammar: Grammar) -> SeedTreeNode:
        self.value = not self.value
        return self

    def to_text(self) -> str:
        return "true" if self.value else "false"

    def to_json_value(self) -> object:
        return self.value


class JsonNullNode(SeedTreeNode):
    def mutate_self(self, rng: random.Random, grammar: Grammar) -> SeedTreeNode:
        return rng.choice(
            [
                JsonBoolNode(False),
                JsonNumberNode(0),
                JsonStringNode(""),
                JsonArrayNode([]),
            ]
        )

    def to_text(self) -> str:
        return "null"

    def to_json_value(self) -> object:
        return None


@dataclass
class JsonArrayNode(SeedTreeNode):
    items: list[SeedTreeNode]

    def walk_mutable_nodes(self) -> list[SeedTreeNode]:
        nodes: list[SeedTreeNode] = [self]
        for item in self.items:
            nodes.extend(item.walk_mutable_nodes())
        return nodes

    def mutate_self(self, rng: random.Random, grammar: Grammar) -> SeedTreeNode:
        action = rng.choice(["append", "drop", "duplicate"])
        if action == "append":
            self.items.append(_generate_json_seed_subtree(grammar, "value", rng))
        elif action == "drop" and self.items:
            self.items.pop(rng.randrange(len(self.items)))
        elif action == "duplicate" and self.items:
            self.items.insert(rng.randrange(len(self.items) + 1), copy.deepcopy(rng.choice(self.items)))
        return self

    def to_text(self) -> str:
        return json.dumps(self.to_json_value(), separators=(",", ":"))

    def to_json_value(self) -> object:
        return [item.to_json_value() for item in self.items]


@dataclass
class JsonPairNode(SeedTreeNode):
    key: JsonStringNode
    value: SeedTreeNode

    def walk_mutable_nodes(self) -> list[SeedTreeNode]:
        return [self, *self.key.walk_mutable_nodes(), *self.value.walk_mutable_nodes()]

    def mutate_self(self, rng: random.Random, grammar: Grammar) -> SeedTreeNode:
        action = rng.choice(["key", "value", "replace_value"])
        if action == "key":
            self.key = copy.deepcopy(self.key)
            self.key.mutate_self(rng, grammar)
        elif action == "replace_value":
            self.value = _generate_json_seed_subtree(grammar, "value", rng)
        else:
            self.value = copy.deepcopy(self.value)
            self.value = self.value.mutate_self(rng, grammar)
        return self

    def to_text(self) -> str:
        return f"{self.key.to_text()}:{self.value.to_text()}"

    def to_json_value(self) -> object:
        return (self.key.value, self.value.to_json_value())


@dataclass
class JsonObjectNode(SeedTreeNode):
    pairs: list[JsonPairNode]

    def walk_mutable_nodes(self) -> list[SeedTreeNode]:
        nodes: list[SeedTreeNode] = [self]
        for pair in self.pairs:
            nodes.extend(pair.walk_mutable_nodes())
        return nodes

    def mutate_self(self, rng: random.Random, grammar: Grammar) -> SeedTreeNode:
        action = rng.choice(["add_pair", "drop_pair", "duplicate_pair"])
        if action == "add_pair":
            key_seed = _generate_json_seed_subtree(grammar, "string", rng)
            if isinstance(key_seed, JsonStringNode):
                key = key_seed
            else:
                key = JsonStringNode("k")
            self.pairs.append(JsonPairNode(key=key, value=_generate_json_seed_subtree(grammar, "value", rng)))
        elif action == "drop_pair" and self.pairs:
            self.pairs.pop(rng.randrange(len(self.pairs)))
        elif action == "duplicate_pair" and self.pairs:
            self.pairs.insert(rng.randrange(len(self.pairs) + 1), copy.deepcopy(rng.choice(self.pairs)))
        return self

    def to_text(self) -> str:
        return json.dumps(self.to_json_value(), separators=(",", ":"))

    def to_json_value(self) -> object:
        out: dict[str, object] = {}
        for pair in self.pairs:
            key, value = pair.to_json_value()
            out[str(key)] = value
        return out


@dataclass
class IPv4SeedNode(SeedTreeNode):
    octets: list[str]
    prefix: str | None = None

    def mutate_self(self, rng: random.Random, grammar: Grammar) -> SeedTreeNode:
        action = rng.choice(["octet", "prefix", "separator"])
        if action == "octet" and self.octets:
            idx = rng.randrange(len(self.octets))
            generated = generate_from_rule(
                start_rule="octet",
                rng=rng,
                count=1,
                preferred_rule_names=["octet"],
            )
            if generated:
                self.octets[idx] = generated[0]
        elif action == "prefix":
            if self.prefix is None:
                generated = generate_from_rule(
                    start_rule="cidr4",
                    rng=rng,
                    count=1,
                    preferred_rule_names=["cidr4"],
                )
                self.prefix = generated[0] if generated else str(rng.randint(0, 32))
            else:
                self.prefix = str(max(-1, min(64, int(self.prefix) + rng.randint(-8, 8))))
        elif self.octets:
            idx = rng.randrange(len(self.octets))
            self.octets[idx] = self.octets[idx] + rng.choice([".", ":", "/"])
        return self

    def to_text(self) -> str:
        body = ".".join(self.octets)
        return f"{body}/{self.prefix}" if self.prefix is not None else body


@dataclass
class IPv6SeedNode(SeedTreeNode):
    hextets: list[str]
    prefix: str | None = None

    def mutate_self(self, rng: random.Random, grammar: Grammar) -> SeedTreeNode:
        action = rng.choice(["hextet", "prefix", "separator"])
        if action == "hextet" and self.hextets:
            idx = rng.randrange(len(self.hextets))
            generated = generate_from_rule(
                start_rule="hex",
                rng=rng,
                count=1,
                preferred_rule_names=["hex"],
            )
            if generated:
                self.hextets[idx] = generated[0]
        elif action == "prefix":
            if self.prefix is None:
                generated = generate_from_rule(
                    start_rule="cidr6",
                    rng=rng,
                    count=1,
                    preferred_rule_names=["cidr6"],
                )
                self.prefix = generated[0] if generated else str(rng.randint(0, 128))
            else:
                self.prefix = str(max(-1, min(256, int(self.prefix) + rng.randint(-16, 16))))
        elif self.hextets:
            idx = rng.randrange(len(self.hextets))
            self.hextets[idx] = self.hextets[idx] + rng.choice([":", "/", "."])
        return self

    def to_text(self) -> str:
        body = ":".join(self.hextets)
        return f"{body}/{self.prefix}" if self.prefix is not None else body


def build() -> Grammar:
    """Build the default JSON and IP grammar used by grammar_ast."""
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


def _apply_extra_rules(grammar: Grammar) -> None:
    """Load additional DSL rules from the configured rules file."""
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


@lru_cache(maxsize=1)
def _base_grammar() -> Grammar:
    """Return the cached base grammar with any external rules applied."""
    grammar = build()
    _apply_extra_rules(grammar)
    return grammar


def _expand_preferred_rules(grammar: Grammar, names: list[str]) -> list[str]:
    """Expand preferred rule names to include existing related overlay names."""
    ordered: list[str] = []
    seen: set[str] = set()
    for name in names:
        for candidate in (name, f"{name}_mut"):
            if candidate in grammar.rules and candidate not in seen:
                ordered.append(candidate)
                seen.add(candidate)
    return ordered


def _json_value_to_seed_tree(value: object) -> SeedTreeNode:
    """Convert a parsed Python JSON value into a mutable seed tree."""
    if isinstance(value, dict):
        return JsonObjectNode(
            [
                JsonPairNode(
                    key=JsonStringNode(str(key)),
                    value=_json_value_to_seed_tree(val),
                )
                for key, val in value.items()
            ]
        )
    if isinstance(value, list):
        return JsonArrayNode([_json_value_to_seed_tree(item) for item in value])
    if isinstance(value, str):
        return JsonStringNode(value)
    if isinstance(value, bool):
        return JsonBoolNode(value)
    if value is None:
        return JsonNullNode()
    if isinstance(value, (int, float)):
        return JsonNumberNode(value)
    return JsonStringNode(str(value))


def _parse_json_seed_tree(text: str) -> SeedTreeNode | None:
    """Parse JSON text into a seed tree, or return None on failure."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return _json_value_to_seed_tree(parsed)


def _parse_ip_seed_tree(text: str) -> SeedTreeNode | None:
    """Parse IPv4/IPv6 text into a seed tree, or return None on failure."""
    stripped = text.strip()
    if not stripped:
        return None
    try:
        if "/" in stripped:
            interface = ipaddress.ip_interface(stripped)
            if interface.version == 4:
                return IPv4SeedNode(
                    octets=str(interface.ip).split("."),
                    prefix=str(interface.network.prefixlen),
                )
            return IPv6SeedNode(
                hextets=interface.ip.exploded.split(":"),
                prefix=str(interface.network.prefixlen),
            )
        address = ipaddress.ip_address(stripped)
        if address.version == 4:
            return IPv4SeedNode(octets=str(address).split("."))
        return IPv6SeedNode(hextets=address.exploded.split(":"))
    except ValueError:
        return None


def _parse_seed_tree(text: str, mutator_kind: str) -> SeedTreeNode | None:
    """Dispatch seed parsing by family and return a seed tree when possible."""
    if mutator_kind == "ip":
        return _parse_ip_seed_tree(text)
    return _parse_json_seed_tree(text)


def _generate_json_seed_subtree(grammar: Grammar, start_rule: str, rng: random.Random) -> SeedTreeNode:
    """Generate a JSON-shaped subtree for add/replace mutations inside seed trees."""
    try:
        generated = generate_from_rule(
            start_rule=start_rule,
            rng=rng,
            count=1,
            preferred_rule_names=[start_rule],
        )
    except KeyError:
        generated = []
    if generated:
        tree = _parse_json_seed_tree(generated[0])
        if tree is not None:
            return tree
    if start_rule == "string":
        return JsonStringNode("x")
    if start_rule == "number":
        return JsonNumberNode(0)
    if start_rule == "array":
        return JsonArrayNode([])
    if start_rule == "object":
        return JsonObjectNode([])
    return JsonStringNode("x")


def _mutate_seed_tree(tree: SeedTreeNode, rng: random.Random, grammar: Grammar) -> SeedTreeNode:
    """Pick one parsed seed-tree node and apply its node-type mutation operator."""
    mutable_nodes = tree.walk_mutable_nodes()
    chosen = rng.choice(mutable_nodes)
    if chosen is tree:
        return tree.mutate_self(rng, grammar)

    def _replace(node: SeedTreeNode) -> SeedTreeNode:
        if node is chosen:
            return node.mutate_self(rng, grammar)
        if isinstance(node, ParseTreeNode):
            node.children = [_replace(child) for child in node.children]  # type: ignore[list-item]
            node.text = node.to_text()
            return node
        if isinstance(node, JsonObjectNode):
            node.pairs = [_replace(pair) for pair in node.pairs]  # type: ignore[list-item]
            return node
        if isinstance(node, JsonArrayNode):
            node.items = [_replace(item) for item in node.items]
            return node
        if isinstance(node, JsonPairNode):
            node.key = _replace(node.key)  # type: ignore[assignment]
            node.value = _replace(node.value)
            return node
        return node

    return _replace(copy.deepcopy(tree))


def _choose_ip_profile(text: str, grammar: Grammar, rng: random.Random) -> tuple[str, list[str]]:
    """Infer the most likely IP start rule and preferred rule set from the seed."""
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
    """Infer the most likely JSON start rule and preferred rule set from the seed."""
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
    """Choose the seed profile within the already-known mutator family."""
    if mutator_kind == "ip":
        return _choose_ip_profile(text, grammar, rng)
    return _choose_json_profile(text, grammar)


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
) -> list[str]:
    """Generate unique seedless candidates from the built grammar."""
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
    """Generate unique candidates from a specific start rule."""
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
    """Mutate from one explicit start rule.

    Prefer exact derivation-tree mutation when the seed can be parsed under the
    requested start rule. Fall back to grammar-AST mutation and generation when
    exact parsing is not available.
    """
    if min_mutation_rounds < 0 or max_mutation_rounds < min_mutation_rounds:
        raise ValueError("invalid mutation round bounds")
    base = _base_grammar()
    if start_rule not in base.rules:
        raise KeyError(f"unknown grammar start rule {start_rule!r}")
    if text:
        parsed_tree = parse_from_rule(text=text, start_rule=start_rule)
        if parsed_tree is not None:
            for _attempt in range(8):
                tree: SeedTreeNode = copy.deepcopy(parsed_tree)
                for _ in range(rng.randint(min_mutation_rounds, max_mutation_rounds)):
                    tree = _mutate_seed_tree(tree, rng, base)
                mutated_text = tree.to_text()
                if mutated_text != text:
                    return mutated_text
            return tree.to_text()
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
    """Main mutator entry point.

    Prefer exact parsed seed-tree mutation for JSON/IP seeds. If parsing fails,
    fall back to grammar-AST mutation and generation.
    """
    base = _base_grammar()
    parsed_tree = _parse_seed_tree(text, mutator_kind)
    if parsed_tree is not None:
        for _attempt in range(8):
            tree = copy.deepcopy(parsed_tree)
            mutation_rounds = rng.randint(1, 5)
            for _ in range(mutation_rounds):
                tree = _mutate_seed_tree(tree, rng, base)
            mutated_text = tree.to_text()
            if mutated_text != text:
                return mutated_text
        return tree.to_text()
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
    "generate_from_rule",
    "generate_without_seed",
    "mutate",
    "mutate_from_rule",
    "parse",
    "parse_from_rule",
    "tokenize",
]
