"""
Adaptive All mutator: flatten the rigid 40/30/30 split.
Here, all mutations (semantic, grammar walk, and byte-level havoc) are 
treated as equal "operators". They all compete in a single AdaptiveStrategy,
allowing the fuzzer to dynamically learn the best mix of techniques for the target.
"""
from __future__ import annotations

import atexit
import json
import random
from typing import Any, Callable

# Import everything we need from the core mutator
from ..mutator import (
    JSON_GRAMMAR,
    IP_GRAMMAR,
    mutate_text_with_grammar,
    bit_flip,
    arithmetic_mutation,
    interesting_value_mutation,
    delete_block_mutation,
    clone_block_mutation,
    _GLOBAL_FUZZER,
    _MAX_PENDING
)
from ..operators import JSON_OPERATORS, IP_OPERATORS, AdaptiveStrategy

# 1. Wrappers to make byte havoc mutations look like string operators
def _wrap_byte_mutator(func: Callable[[bytes, random.Random], bytes]) -> Callable[[Any, random.Random], str]:
    def wrapper(data_or_text: Any, rng: random.Random) -> str:
        # If the input is already parsed JSON (dict/list), we need to serialize it first
        text = json.dumps(data_or_text) if isinstance(data_or_text, (dict, list)) else str(data_or_text)
        b = text.encode("utf-8", errors="replace")
        mutated_b = func(data=b, rng=rng)
        return mutated_b.decode("utf-8", errors="ignore")
    return wrapper

# 2. Wrapper to make grammar mutation look like a string operator
def _grammar_operator(grammar_spec):
    def wrapper(data_or_text: Any, rng: random.Random) -> str:
        text = json.dumps(data_or_text) if isinstance(data_or_text, (dict, list)) else str(data_or_text)
        return mutate_text_with_grammar(original_text=text, grammar_spec=grammar_spec, max_depth=5, rng=rng)
    return wrapper

# 3. Create the unified operator dictionaries
ALL_JSON_OPS = dict(JSON_OPERATORS)
ALL_JSON_OPS.update({
    "byte_bit_flip": _wrap_byte_mutator(bit_flip),
    "byte_arithmetic": _wrap_byte_mutator(arithmetic_mutation),
    "byte_interesting_val": _wrap_byte_mutator(interesting_value_mutation),
    "byte_delete_block": _wrap_byte_mutator(delete_block_mutation),
    "byte_clone_block": _wrap_byte_mutator(clone_block_mutation),
    "grammar_splice": _grammar_operator(JSON_GRAMMAR),
})

ALL_IP_OPS = dict(IP_OPERATORS)
ALL_IP_OPS.update({
    "byte_bit_flip": _wrap_byte_mutator(bit_flip),
    "byte_arithmetic": _wrap_byte_mutator(arithmetic_mutation),
    "byte_interesting_val": _wrap_byte_mutator(interesting_value_mutation),
    "byte_delete_block": _wrap_byte_mutator(delete_block_mutation),
    "byte_clone_block": _wrap_byte_mutator(clone_block_mutation),
    "grammar_splice": _grammar_operator(IP_GRAMMAR),
})

# 4. Create our own fuzzer instance that uses the unified pools
class AdaptiveAllFuzzer:
    def __init__(self):
        self.strategies = {
            "json": AdaptiveStrategy(list(ALL_JSON_OPS.keys())),
            "ip": AdaptiveStrategy(list(ALL_IP_OPS.keys())),
        }
        self._pending: dict[str, tuple[str, str]] = {}

    def mutate(self, text: str, grammar_type: str, rng: random.Random) -> str:
        strategy = self.strategies.get(grammar_type)
        if not strategy:
            return text

        op_name = strategy.select_operator(rng)
        operators = ALL_JSON_OPS if grammar_type == "json" else ALL_IP_OPS
        op_func = operators[op_name]

        if grammar_type == "json":
            try:
                data = json.loads(text) if text else {}
                mutated_data = op_func(data, rng)
                result = mutated_data if isinstance(mutated_data, str) else json.dumps(mutated_data)
            except json.JSONDecodeError:
                # If we can't parse it, we must use a string-based operator
                string_ops = [k for k in operators.keys() if k.startswith("byte_") or k == "grammar_splice"]
                fallback_op = rng.choice(string_ops)
                result = operators[fallback_op](text, rng)
                op_name = fallback_op
        else:
            result = op_func(text, rng)

        if len(self._pending) >= _MAX_PENDING:
            self._pending.pop(next(iter(self._pending)))
        self._pending[result] = (grammar_type, op_name)
        return result

    def record_coverage(self, mutated_text: str, gained_coverage: bool) -> None:
        entry = self._pending.pop(mutated_text, None)
        if entry is not None:
            grammar_type, op_name = entry
            self.strategies[grammar_type].update_score(op_name, gained_coverage)

# Global instance for this version
_ALL_FUZZER = AdaptiveAllFuzzer()

# Provide an implementation of record_coverage that mutator/__init__.py can forward to
def handle_coverage_feedback(mutated_text: str, gained_coverage: bool) -> bool:
    """Returns True if this fuzzer handled the feedback."""
    if mutated_text in _ALL_FUZZER._pending:
        _ALL_FUZZER.record_coverage(mutated_text, gained_coverage)
        return True
    return False

def mutate(
    text: str,
    *,
    mutator_kind: str,
    rng: random.Random,
) -> str:
    """Entry point called by the core fuzzer loop."""
    return _ALL_FUZZER.mutate(text, mutator_kind, rng)

def _print_final_probabilities():
    print("\n" + "="*50)
    print(" ADAPTIVE ALL: FINAL OPERATOR PROBABILITIES")
    print("="*50)
    for kind, strategy in _ALL_FUZZER.strategies.items():
        probs = strategy.get_probabilities()
        if not probs:
            continue
        print(f"\n[{kind.upper()} Operators - Top 10]")
        # Sort by highest probability first
        for op, p in sorted(probs.items(), key=lambda x: -x[1])[:10]:
            print(f"  {op:25s}: {p:.4f}")
    print("="*50 + "\n")

atexit.register(_print_final_probabilities)
