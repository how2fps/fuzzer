"""
Adaptive-improved mutator: adaptively chooses among grammar-driven and byte-level
operators using an improved PSO approach with dynamic epsilon and decayed history.
"""
from __future__ import annotations

import random
import threading
from collections.abc import Callable

from .adaptive_improved_operators import AdaptiveImprovedStrategy
from . import grammar_ast
from .lib import (
    apply_grammar_operator,
    arithmetic_mutation,
    available_grammar_operator_names,
    bit_flip,
    clone_block_mutation,
    delete_block_mutation,
    grammar_capabilities,
    interesting_value_mutation,
    mutate_text_with_grammar,
    resolve_grammar_spec,
)

ByteMutator = Callable[..., bytes]
Operator = Callable[[str, random.Random], str]
_DEFAULT_MAX_DEPTH = 5


def _wrap_byte_mutator(func: ByteMutator) -> Operator:
    def wrapper(text: str, rng: random.Random) -> str:
        mutated = func(data=text.encode("utf-8", errors="replace"), rng=rng)
        return mutated.decode("utf-8", errors="ignore")

    return wrapper


def _build_unified_ops(*, mutator_kind: str) -> dict[str, Operator]:
    grammar_spec = resolve_grammar_spec(kind=mutator_kind)
    capabilities = grammar_capabilities(grammar_spec=grammar_spec)
    ops = {
        f"grammar_{operator_name}": (
            lambda text, rng, operator_name=operator_name: apply_grammar_operator(
                operator_name=operator_name,
                original_text=text,
                grammar_spec=grammar_spec,
                max_depth=_DEFAULT_MAX_DEPTH,
                rng=rng,
            )
        )
        for operator_name in available_grammar_operator_names(grammar_spec=grammar_spec)
    }
    if capabilities.has_exact_parse_path:
        ops["grammar_ast_tree_mutate"] = (
            lambda text, rng: grammar_ast.mutate(
                text,
                mutator_kind=mutator_kind,
                rng=rng,
            )
        )
    ops.update(
        {
            "byte_bit_flip": _wrap_byte_mutator(bit_flip),
            "byte_arithmetic": _wrap_byte_mutator(arithmetic_mutation),
            "byte_interesting_val": _wrap_byte_mutator(interesting_value_mutation),
            "byte_delete_block": _wrap_byte_mutator(delete_block_mutation),
            "byte_clone_block": _wrap_byte_mutator(clone_block_mutation),
        }
    )
    return ops


class AdaptiveImprovedMutator:
    def __init__(self) -> None:
        self._strategy_by_kind: dict[str, AdaptiveImprovedStrategy] = {}
        self._pending_by_text: dict[str, tuple[str, str]] = {}
        self._lock = threading.Lock()

    def _strategy_for_kind(
        self,
        *,
        mutator_kind: str,
        operator_names: list[str],
    ) -> AdaptiveImprovedStrategy:
        strategy = self._strategy_by_kind.get(mutator_kind)
        if strategy is None or (
            isinstance(strategy, AdaptiveImprovedStrategy)
            and set(strategy.weights) != set(operator_names)
        ):
            strategy = AdaptiveImprovedStrategy(operator_names)
            self._strategy_by_kind[mutator_kind] = strategy
        return strategy

    def mutate(self, text: str, *, mutator_kind: str, rng: random.Random) -> str:
        ops = _build_unified_ops(mutator_kind=mutator_kind)
        operator_names = list(ops)
        with self._lock:
            strategy = self._strategy_for_kind(
                mutator_kind=mutator_kind,
                operator_names=operator_names,
            )
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
            if isinstance(strategy, AdaptiveImprovedStrategy) and op_name not in strategy.weights:
                return False
            strategy.update_score(op_name, gained_coverage)
            return True


_MUTATOR = AdaptiveImprovedMutator()


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
