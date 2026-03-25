"""
Grammar-AST mutator.

This version mutates a small grammar represented as an AST of generic node
types (Literal / Sequence / Alternation / Repeat / Ref), then generates a new
candidate from that mutated grammar. If an original seed is present, a final
seed-guided splice step keeps some local continuity with the incoming text.
"""
from __future__ import annotations

import copy
import random
from dataclasses import dataclass


class Node:
    def generate(self, grammar: dict[str, "Node"], rng: random.Random) -> str:
        raise NotImplementedError

    def mutate(self, grammar: dict[str, "Node"], rng: random.Random) -> "Node":
        return self

    def clone(self) -> "Node":
        return copy.deepcopy(self)


@dataclass
class Literal(Node):
    value: str

    def generate(self, grammar: dict[str, Node], rng: random.Random) -> str:
        return self.value

    def mutate(self, grammar: dict[str, Node], rng: random.Random) -> Node:
        if not self.value:
            return self
        action = rng.choice(("replace", "drop", "keep"))
        if action == "replace":
            idx = rng.randrange(len(self.value))
            replacement = chr(rng.randint(32, 126))
            self.value = self.value[:idx] + replacement + self.value[idx + 1 :]
        elif action == "drop" and len(self.value) > 1:
            idx = rng.randrange(len(self.value))
            self.value = self.value[:idx] + self.value[idx + 1 :]
        return self


@dataclass
class Sequence(Node):
    nodes: list[Node]

    def generate(self, grammar: dict[str, Node], rng: random.Random) -> str:
        return "".join(node.generate(grammar, rng) for node in self.nodes)

    def mutate(self, grammar: dict[str, Node], rng: random.Random) -> Node:
        if not self.nodes:
            return self
        action = rng.choice(("swap", "delete", "mutate_child", "duplicate"))
        if action == "swap" and len(self.nodes) > 1:
            i, j = rng.sample(range(len(self.nodes)), 2)
            self.nodes[i], self.nodes[j] = self.nodes[j], self.nodes[i]
        elif action == "delete" and len(self.nodes) > 1:
            self.nodes.pop(rng.randrange(len(self.nodes)))
        elif action == "duplicate":
            node = self.nodes[rng.randrange(len(self.nodes))].clone()
            self.nodes.insert(rng.randrange(len(self.nodes) + 1), node)
        else:
            rng.choice(self.nodes).mutate(grammar, rng)
        return self


@dataclass
class Alternation(Node):
    options: list[Node]

    def generate(self, grammar: dict[str, Node], rng: random.Random) -> str:
        return rng.choice(self.options).generate(grammar, rng)

    def mutate(self, grammar: dict[str, Node], rng: random.Random) -> Node:
        if not self.options:
            return self
        rng.choice(self.options).mutate(grammar, rng)
        return self


@dataclass
class Repeat(Node):
    node: Node
    min_r: int
    max_r: int

    def generate(self, grammar: dict[str, Node], rng: random.Random) -> str:
        count = rng.randint(self.min_r, self.max_r)
        return "".join(self.node.generate(grammar, rng) for _ in range(count))

    def mutate(self, grammar: dict[str, Node], rng: random.Random) -> Node:
        if rng.random() < 0.5:
            self.max_r = min(self.max_r + rng.randint(1, 2), 12)
        if rng.random() < 0.5 and self.min_r > 0:
            self.min_r = max(0, self.min_r - 1)
        self.node.mutate(grammar, rng)
        if self.max_r < self.min_r:
            self.max_r = self.min_r
        return self


@dataclass
class Ref(Node):
    name: str

    def generate(self, grammar: dict[str, Node], rng: random.Random) -> str:
        return grammar[self.name].generate(grammar, rng)

    def mutate(self, grammar: dict[str, Node], rng: random.Random) -> Node:
        grammar[self.name].mutate(grammar, rng)
        return self


def _lit(value: str) -> Literal:
    return Literal(value)


def _build_json_grammar() -> dict[str, Node]:
    string_chars = Alternation(
        [_lit("a"), _lit("b"), _lit("json"), _lit("x"), _lit("0"), _lit(" ")]
    )
    return {
        "start": Ref("object"),
        "string": Sequence([_lit('"'), Repeat(string_chars, 1, 4), _lit('"')]),
        "number": Alternation(
            [_lit("0"), _lit("1"), _lit("-1"), _lit("42"), _lit("3.14"), _lit("1e10")]
        ),
        "value": Alternation([Ref("string"), Ref("number"), _lit("true"), _lit("false"), _lit("null")]),
        "pair": Sequence([Ref("string"), _lit(":"), Ref("value")]),
        "object": Sequence(
            [
                _lit("{"),
                Ref("pair"),
                Repeat(Sequence([_lit(","), Ref("pair")]), 0, 3),
                _lit("}"),
            ]
        ),
    }


def _build_ip_grammar() -> dict[str, Node]:
    octet = Alternation([_lit("0"), _lit("1"), _lit("10"), _lit("127"), _lit("192"), _lit("255")])
    hextet = Alternation([_lit("0"), _lit("1"), _lit("a"), _lit("f"), _lit("10"), _lit("ffff")])
    return {
        "start": Alternation([Ref("ipv4_cidr"), Ref("ipv6_cidr")]),
        "ipv4": Sequence(
            [
                octet.clone(),
                _lit("."),
                octet.clone(),
                _lit("."),
                octet.clone(),
                _lit("."),
                octet.clone(),
            ]
        ),
        "ipv4_prefix": Alternation([_lit("8"), _lit("16"), _lit("24"), _lit("32")]),
        "ipv4_cidr": Sequence([Ref("ipv4"), _lit("/"), Ref("ipv4_prefix")]),
        "ipv6": Sequence(
            [
                hextet.clone(), _lit(":"), hextet.clone(), _lit(":"), hextet.clone(), _lit(":"),
                hextet.clone(), _lit(":"), hextet.clone(), _lit(":"), hextet.clone(), _lit(":"),
                hextet.clone(), _lit(":"), hextet.clone(),
            ]
        ),
        "ipv6_prefix": Alternation([_lit("32"), _lit("48"), _lit("64"), _lit("96"), _lit("128")]),
        "ipv6_cidr": Sequence([Ref("ipv6"), _lit("/"), Ref("ipv6_prefix")]),
    }


def _build_grammar_ast(mutator_kind: str) -> tuple[dict[str, Node], str]:
    if mutator_kind == "ip":
        return _build_ip_grammar(), "start"
    return _build_json_grammar(), "start"


def _seed_guided_splice(*, original_text: str, generated_text: str, rng: random.Random) -> str:
    if not original_text:
        return generated_text
    if not generated_text:
        return original_text

    action = rng.choice(("replace_middle", "insert_generated", "keep_generated"))
    if action == "keep_generated":
        return generated_text

    start = rng.randrange(len(original_text))
    end = rng.randrange(start, len(original_text))
    if action == "replace_middle":
        return original_text[:start] + generated_text + original_text[end:]
    return original_text[:start] + generated_text + original_text[start:]


def mutate(
    text: str,
    *,
    mutator_kind: str,
    rng: random.Random,
) -> str:
    """
    Mutate by perturbing a grammar AST, generating a candidate, then optionally
    splicing that candidate with the incoming seed.
    """
    grammar, start_rule = _build_grammar_ast(mutator_kind)
    mutation_rounds = rng.randint(1, 5)
    for _ in range(mutation_rounds):
        rng.choice(list(grammar.values())).mutate(grammar, rng)

    generated = grammar[start_rule].generate(grammar, rng)
    if text:
        return _seed_guided_splice(original_text=text, generated_text=generated, rng=rng)
    return generated
