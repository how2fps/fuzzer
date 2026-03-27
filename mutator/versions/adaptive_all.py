"""
Adaptive-all mutator: treats JSON, IP, grammar, and byte-level mutations as a
single operator pool and picks among them with an adaptive strategy.
"""
from __future__ import annotations

import json
import random
import threading
from collections.abc import Callable
from typing import Any

from .adaptive_operators import AdaptiveStrategy, IP_OPERATORS, JSON_OPERATORS
from .lib import (
    IP_GRAMMAR,
    JSON_GRAMMAR,
    arithmetic_mutation,
    bit_flip,
    clone_block_mutation,
    delete_block_mutation,
    interesting_value_mutation,
    mutate_text_with_grammar,
)

ByteMutator = Callable[..., bytes]
Operator = Callable[[Any, random.Random], str]


def _wrap_byte_mutator(func: ByteMutator) -> Operator:
    def wrapper(data_or_text: Any, rng: random.Random) -> str:
        if isinstance(data_or_text, (dict, list)):
            text = json.dumps(data_or_text)
        else:
            text = str(data_or_text)
        mutated = func(data=text.encode("utf-8", errors="replace"), rng=rng)
        return mutated.decode("utf-8", errors="ignore")

    return wrapper


def _grammar_operator(grammar_spec: dict[str, object]) -> Operator:
    def wrapper(data_or_text: Any, rng: random.Random) -> str:
        if isinstance(data_or_text, (dict, list)):
            text = json.dumps(data_or_text)
        else:
            text = str(data_or_text)
        return mutate_text_with_grammar(
            original_text=text,
            grammar_spec=grammar_spec,
            max_depth=5,
            rng=rng,
        )

    return wrapper


def _json_semantic_wrapper(op_func: Callable[[Any, random.Random], Any]) -> Operator:
    def wrapper(text: Any, rng: random.Random) -> str:
        parsed = json.loads(text if isinstance(text, str) else str(text))
        mutated = op_func(parsed, rng)
        if isinstance(mutated, str):
            return mutated
        return json.dumps(mutated)

    return wrapper


def _build_unified_ops() -> dict[str, Operator]:
    unified: dict[str, Operator] = {}
    for name, func in JSON_OPERATORS.items():
        unified[f"json_{name}"] = _json_semantic_wrapper(func)
    for name, func in IP_OPERATORS.items():
        unified[f"ip_{name}"] = func
    unified.update(
        {
            "byte_bit_flip": _wrap_byte_mutator(bit_flip),
            "byte_arithmetic": _wrap_byte_mutator(arithmetic_mutation),
            "byte_interesting_val": _wrap_byte_mutator(interesting_value_mutation),
            "byte_delete_block": _wrap_byte_mutator(delete_block_mutation),
            "byte_clone_block": _wrap_byte_mutator(clone_block_mutation),
            "json_grammar_cache": _grammar_operator(JSON_GRAMMAR),
            "ip_grammar_cache": _grammar_operator(IP_GRAMMAR),
        }
    )
    return unified


UNIFIED_OPS = _build_unified_ops()


class AdaptiveAllMutator:
    def __init__(self) -> None:
        self.strategy = AdaptiveStrategy(list(UNIFIED_OPS.keys()))
        self._pending_by_text: dict[str, str] = {}
        self._lock = threading.Lock()

    def mutate(self, text: str, *, mutator_kind: str, rng: random.Random) -> str:
        with self._lock:
            op_name = self.strategy.select_operator(rng)
        try:
            mutated = UNIFIED_OPS[op_name](text, rng)
        except Exception:
            fallback_grammar = IP_GRAMMAR if mutator_kind == "ip" else JSON_GRAMMAR
            mutated = mutate_text_with_grammar(
                original_text=text,
                grammar_spec=fallback_grammar,
                max_depth=5,
                rng=rng,
            )
        with self._lock:
            self._pending_by_text[mutated] = op_name
        return mutated

    def handle_feedback(self, *, mutated_text: str, gained_coverage: bool) -> bool:
        with self._lock:
            op_name = self._pending_by_text.pop(mutated_text, None)
            if op_name is None:
                return False
            self.strategy.update_score(op_name, gained_coverage)
            return True


_MUTATOR = AdaptiveAllMutator()


def mutate(
    text: str,
    *,
    mutator_kind: str,
    rng: random.Random,
) -> str:
    return _MUTATOR.mutate(text, mutator_kind=mutator_kind, rng=rng)


def handle_feedback(*, mutated_text: str, gained_coverage: bool) -> bool:
    return _MUTATOR.handle_feedback(
        mutated_text=mutated_text,
        gained_coverage=gained_coverage,
    )
