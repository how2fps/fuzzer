"""
Adaptive-all mutator: adaptively chooses among grammar-driven and byte-level
operators using the active runtime grammar.
"""
from __future__ import annotations

import random
import threading
from collections.abc import Callable

from .adaptive_operators import AdaptiveStrategy
from .lib import (
    arithmetic_mutation,
    bit_flip,
    clone_block_mutation,
    delete_block_mutation,
    generate_from_grammar,
    interesting_value_mutation,
    mutate_text_with_grammar,
    resolve_grammar_spec,
)

ByteMutator = Callable[..., bytes]
Operator = Callable[[str, random.Random], str]
_OPERATOR_NAMES = (
    "grammar_splice",
    "grammar_regenerate",
    "byte_bit_flip",
    "byte_arithmetic",
    "byte_interesting_val",
    "byte_delete_block",
    "byte_clone_block",
)
_DEFAULT_MAX_DEPTH = 5


def _wrap_byte_mutator(func: ByteMutator) -> Operator:
    def wrapper(text: str, rng: random.Random) -> str:
        mutated = func(data=text.encode("utf-8", errors="replace"), rng=rng)
        return mutated.decode("utf-8", errors="ignore")

    return wrapper


def _build_unified_ops(*, mutator_kind: str) -> dict[str, Operator]:
    grammar_spec = resolve_grammar_spec(kind=mutator_kind)
    return {
        "grammar_splice": lambda text, rng: mutate_text_with_grammar(
            original_text=text,
            grammar_spec=grammar_spec,
            max_depth=_DEFAULT_MAX_DEPTH,
            rng=rng,
        ),
        "grammar_regenerate": lambda _text, rng: generate_from_grammar(
            grammar_spec=grammar_spec,
            max_depth=_DEFAULT_MAX_DEPTH,
            rng=rng,
        ),
        "byte_bit_flip": _wrap_byte_mutator(bit_flip),
        "byte_arithmetic": _wrap_byte_mutator(arithmetic_mutation),
        "byte_interesting_val": _wrap_byte_mutator(interesting_value_mutation),
        "byte_delete_block": _wrap_byte_mutator(delete_block_mutation),
        "byte_clone_block": _wrap_byte_mutator(clone_block_mutation),
    }


class AdaptiveAllMutator:
    def __init__(self) -> None:
        self._strategy_by_kind: dict[str, AdaptiveStrategy] = {}
        self._pending_by_text: dict[str, tuple[str, str]] = {}
        self._lock = threading.Lock()

    def _strategy_for_kind(self, *, mutator_kind: str) -> AdaptiveStrategy:
        strategy = self._strategy_by_kind.get(mutator_kind)
        if strategy is None:
            strategy = AdaptiveStrategy(list(_OPERATOR_NAMES))
            self._strategy_by_kind[mutator_kind] = strategy
        return strategy

    def mutate(self, text: str, *, mutator_kind: str, rng: random.Random) -> str:
        ops = _build_unified_ops(mutator_kind=mutator_kind)
        with self._lock:
            strategy = self._strategy_for_kind(mutator_kind=mutator_kind)
            op_name = strategy.select_operator(rng)
        try:
            mutated = ops[op_name](text, rng)
        except Exception:
            grammar_spec = resolve_grammar_spec(kind=mutator_kind)
            mutated = mutate_text_with_grammar(
                original_text=text,
                grammar_spec=grammar_spec,
                max_depth=_DEFAULT_MAX_DEPTH,
                rng=rng,
            )
        with self._lock:
            self._pending_by_text[mutated] = (mutator_kind, op_name)
        return mutated

    def handle_feedback(self, *, mutated_text: str, gained_coverage: bool) -> bool:
        with self._lock:
            pending = self._pending_by_text.pop(mutated_text, None)
            if pending is None:
                return False
            mutator_kind, op_name = pending
            strategy = self._strategy_by_kind.get(mutator_kind)
            if strategy is None:
                return False
            strategy.update_score(op_name, gained_coverage)
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
