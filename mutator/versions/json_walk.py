"""
JSON-walk mutator: structurally mutates JSON inputs using a small hand-written
grammar and a "walk + havoc" strategy adapted from the standalone fuzzer
prototype in ``fuzzer/mutator.py``.
"""
from __future__ import annotations

import json
import random
from typing import Any, Callable


TerminalFn = Callable[[random.Random], str]
GrammarTransition = tuple[TerminalFn | str, str]
GrammarMap = dict[str, list[GrammarTransition]]


def _gen_random_str(rng: random.Random) -> str:
    length = rng.randint(1, 25)
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(rng.choice(alphabet) for _ in range(length))


def _gen_int(rng: random.Random) -> str:
    return str(rng.choice((0, -1, 2_147_483_647, rng.randint(0, 1000))))


def _json_to_walk(data: Any) -> list[tuple[str, str]]:
    """
    Convert a parsed JSON value into a linear "walk" of (state, terminal) pairs.

    This is intentionally lossy and only used as a starting point for havoc;
    the grammar-driven generator will diverge from some state along this walk.
    """
    walk: list[tuple[str, str]] = []

    if isinstance(data, dict):
        walk.append(("VALUE", "{"))
        items = list(data.items())
        for i, (key, value) in enumerate(items):
            walk.append(("STR_BODY", str(key)))
            walk.append(("OBJ_BODY", ":"))
            walk.extend(_json_to_walk(value))
            if i < len(items) - 1:
                walk.append(("NEXT_OBJ", ","))
        walk.append(("FINAL", "}"))
        return walk

    if isinstance(data, list):
        walk.append(("VALUE", "["))
        for i, value in enumerate(data):
            walk.extend(_json_to_walk(value))
            if i < len(data) - 1:
                walk.append(("ARR_NEXT", ","))
        walk.append(("FINAL", "]"))
        return walk

    if isinstance(data, str):
        walk.append(("STR_BODY", data))
        return walk

    if isinstance(data, (int, float, bool)) or data is None:
        walk.append(("VALUE", json.dumps(data)))
        return walk

    # Fallback: string representation
    walk.append(("VALUE", str(data)))
    return walk


_MORE_CORRECT_JSON_GRAMMAR_MAP: GrammarMap = {
    "VALUE": [("{", "OBJ_BODY"), ("[", "ARR_BODY")],
    "OBJ_BODY": [('"', "STR_START")],
    "STR_START": [(_gen_random_str, "STR_END")],
    "STR_END": [('"', "COLON")],
    "COLON": [(":", "VAL")],
    "VAL": [
        (_gen_int, "OBJ_BRANCH"),
        ("true", "OBJ_BRANCH"),
        ("{", "OBJ_BODY_NESTED"),
        ("[", "ARR_BODY_NESTED"),
    ],
    "OBJ_BRANCH": [("}", "FINAL"), (",", "OBJ_BODY")],
    "ARR_BODY": [
        ("]", "FINAL"),
        (_gen_int, "ARR_BRANCH"),
        ("{", "OBJ_BODY"),
        ("[", "ARR_BODY"),
    ],
    "ARR_BRANCH": [("]", "FINAL"), (",", "VAL_IN_ARR")],
    "VAL_IN_ARR": [
        (_gen_int, "ARR_BRANCH"),
        ("{", "OBJ_BODY"),
        ("[", "ARR_BODY"),
    ],
    "OBJ_BODY_NESTED": [('"', "STR_START")],
    "ARR_BODY_NESTED": [
        ("]", "FINAL"),
        (_gen_int, "ARR_BRANCH"),
        ("{", "OBJ_BODY"),
        ("[", "ARR_BODY"),
    ],
    "FINAL": [],
}


def _generate_walk_from_state(
    *,
    grammar: GrammarMap,
    start_state: str,
    end_state: str = "FINAL",
    rng: random.Random,
    max_steps: int = 256,
) -> list[tuple[str, str]]:
    """
    Generate a grammar walk starting from start_state, keeping braces/brackets balanced.
    """
    walk: list[tuple[str, str]] = []
    stack: list[str] = []
    current_state = start_state
    steps = 0

    while (
        current_state != end_state
        and current_state in grammar
        and steps < max_steps
    ):
        choices = list(grammar[current_state])
        if not choices:
            break

        # Avoid starting with a closing brace/bracket and avoid mismatched closer.
        if not stack:
            choices = [c for c in choices if c[0] not in ("}", "]")]
        else:
            expected_closer = stack[-1]
            forbidden = {"}": "]", "]": "}"}.get(expected_closer)
            if forbidden is not None:
                choices = [c for c in choices if c[0] != forbidden]

        if not choices:
            break

        terminal_choice, next_state = rng.choice(choices)
        terminal = (
            terminal_choice(rng)
            if callable(terminal_choice)
            else terminal_choice
        )

        if terminal == "{":
            stack.append("}")
        elif terminal == "[":
            stack.append("]")
        elif terminal in ("}", "]") and stack:
            stack.pop()

        walk.append((current_state, str(terminal)))
        current_state = next_state
        steps += 1

    while stack and steps < max_steps:
        closer = stack.pop()
        walk.append(("FORCE_CLOSE", closer))
        steps += 1

    return walk


def _mutate_random(
    walk: list[tuple[str, str]],
    *,
    grammar: GrammarMap,
    rng: random.Random,
) -> list[tuple[str, str]]:
    if not walk:
        return _generate_walk_from_state(
            grammar=grammar,
            start_state="VALUE",
            end_state="FINAL",
            rng=rng,
        )
    split_idx = rng.randrange(len(walk))
    state_to_diverge_from = walk[split_idx][0]
    prefix = walk[:split_idx]
    suffix = _generate_walk_from_state(
        grammar=grammar,
        start_state=state_to_diverge_from,
        end_state="FINAL",
        rng=rng,
    )
    return prefix + suffix


def _mutate_splice(
    walk1: list[tuple[str, str]],
    walk2: list[tuple[str, str]],
    *,
    rng: random.Random,
) -> list[tuple[str, str]]:
    states1: dict[tuple[str, str], int] = {
        step: i for i, step in enumerate(walk1) if step is not None
    }
    common_indices = [j for j, step in enumerate(walk2) if step in states1]
    if not common_indices:
        return walk1
    w2_idx = rng.choice(common_indices)
    shared_step = walk2[w2_idx]
    w1_idx = states1[shared_step]
    return walk1[:w1_idx] + walk2[w2_idx:]


def _havoc_walk(
    walk: list[tuple[str, str]],
    *,
    grammar: GrammarMap,
    corpus: list[list[tuple[str, str]]],
    rng: random.Random,
) -> list[tuple[str, str]]:
    if not walk:
        return _generate_walk_from_state(
            grammar=grammar,
            start_state="VALUE",
            end_state="FINAL",
            rng=rng,
        )

    mutated = list(walk)
    num_mutations = 1 << rng.randint(1, 4)  # 2–16 mutations

    for _ in range(num_mutations):
        strategies: list[str] = ["random"]
        if len(corpus) > 1:
            strategies.append("splice")
        strategy = rng.choice(strategies)
        if strategy == "splice":
            other = rng.choice(corpus)
            mutated = _mutate_splice(mutated, other, rng=rng)
        else:
            mutated = _mutate_random(mutated, grammar=grammar, rng=rng)
    return mutated


def _unparse_walk(walk: list[tuple[str, str]]) -> str:
    return "".join(value for _, value in walk)


def _finalize_structure(s: str) -> str:
    braces = s.count("{") - s.count("}")
    brackets = s.count("[") - s.count("]")
    s = s.rstrip(",:")
    if braces > 0:
        s += "}" * braces
    if brackets > 0:
        s += "]" * brackets
    return s


def mutate(
    text: str,
    *,
    mutator_kind: str,
    rng: random.Random,
) -> str:
    """
    Mutate JSON input using the walk-based grammar mutator.

    Only applies when mutator_kind == "json"; for other kinds the input is
    returned unchanged so that this mutator can be used selectively.
    """
    if mutator_kind != "json":
        return text

    try:
        parsed = json.loads(text) if text else None
    except json.JSONDecodeError:
        parsed = None

    if parsed is None:
        base_walk: list[tuple[str, str]] = []
        corpus: list[list[tuple[str, str]]] = []
    else:
        base_walk = _json_to_walk(parsed)
        corpus = [base_walk]

    mutated_walk = _havoc_walk(
        base_walk,
        grammar=_MORE_CORRECT_JSON_GRAMMAR_MAP,
        corpus=corpus,
        rng=rng,
    )
    raw = _unparse_walk(mutated_walk)
    return _finalize_structure(raw)

