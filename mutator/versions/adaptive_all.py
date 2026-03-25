"""
Adaptive All mutator: flatten the rigid 40/30/30 split.
Here, all mutations (semantic, grammar walk, and byte-level havoc) are 
treated as equal "operators". They all compete in a single AdaptiveStrategy,
allowing the fuzzer to dynamically learn the best mix of techniques for the target.
"""
from __future__ import annotations

import atexit
import orjson as json
import random
import os
import time
from multiprocessing import current_process
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
    _MAX_PENDING,
    load_grammar_from_json
)
from ..operators import JSON_OPERATORS, IP_OPERATORS, AdaptiveStrategy

# 1. Wrappers to make byte havoc mutations look like string operators
def _wrap_byte_mutator(func: Callable[[bytes, random.Random], bytes]) -> Callable[[Any, random.Random], str]:
    def wrapper(data_or_text: Any, rng: random.Random) -> str:
        # If the input is already parsed JSON (dict/list), we need to serialize it first
        if isinstance(data_or_text, (dict, list)):
            text = json.dumps(data_or_text).decode("utf-8", errors="ignore")
        else:
            text = str(data_or_text)
            
        b = text.encode("utf-8", errors="replace")
        mutated_b = func(data=b, rng=rng)
        return mutated_b.decode("utf-8", errors="ignore")
    return wrapper

# 2. Wrapper to make grammar mutation look like a string operator
def _grammar_operator(grammar_spec):
    def wrapper(data_or_text: Any, rng: random.Random) -> str:
        if isinstance(data_or_text, (dict, list)):
            text = json.dumps(data_or_text).decode("utf-8", errors="ignore")
        else:
            text = str(data_or_text)
        return mutate_text_with_grammar(original_text=text, grammar_spec=grammar_spec, max_depth=5, rng=rng)
    return wrapper

# 3. Create the unified operator pool
UNIFIED_OPS = {}

def json_semantic_wrapper(op_func):
    """Parses JSON text into dict/list and applies a JSON operator."""
    def wrapper(text: str, rng: random.Random) -> str:
        # JSON operators require parsed Python objects
        data = json.loads(text)
        mutated_data = op_func(data, rng)
        # Prevent double-encoding if the operator returned raw JSON text (e.g. duplicate_keys)
        if isinstance(mutated_data, str):
            return mutated_data
        return json.dumps(mutated_data).decode("utf-8", errors="ignore")
    return wrapper

# Add JSON operators with prefix
for name, func in JSON_OPERATORS.items():
    UNIFIED_OPS[f"json_{name}"] = json_semantic_wrapper(func)

# Add IP operators with prefix
for name, func in IP_OPERATORS.items():
    UNIFIED_OPS[f"ip_{name}"] = func

# Add Byte Havoc operators
UNIFIED_OPS.update({
    "byte_bit_flip": _wrap_byte_mutator(bit_flip),
    "byte_arithmetic": _wrap_byte_mutator(arithmetic_mutation),
    "byte_interesting_val": _wrap_byte_mutator(interesting_value_mutation),
    "byte_delete_block": _wrap_byte_mutator(delete_block_mutation),
    "byte_clone_block": _wrap_byte_mutator(clone_block_mutation),
})

# Add Grammar Splices (explicitly distinguished)
UNIFIED_OPS["json_grammar_cache"] = _grammar_operator(JSON_GRAMMAR)
UNIFIED_OPS["ip_grammar_cache"] = _grammar_operator(IP_GRAMMAR)

# 4. Create our own fuzzer instance that uses the unified pool
class AdaptiveAllFuzzer:
    def __init__(self):
        self._init_strategy()
        # Maps mutated_text -> op_name for deferred feedback.
        self._pending = {} 
        self._counter = 0

    def _init_strategy(self):
        self.strategy = AdaptiveStrategy(list(UNIFIED_OPS.keys()))

    def add_grammar_operator(self, name: str, grammar_spec: Any):
        """Adds a new grammar-based operator to the pool and re-inits strategy."""
        op_name = f"custom_grammar_{name}"
        UNIFIED_OPS[op_name] = _grammar_operator(grammar_spec)
        self._init_strategy()

    def mutate(self, text: str, grammar_type: str, rng: random.Random) -> str:
        # We ignore grammar_type and just pick from the unified pool
        op_name = self.strategy.select_operator(rng)
        
        # If the operator is byte-level, it expects bytes. 
        # But wait, our wrappers already handle the text/json conversion.
        # Let's ensure the input to the wrapper is consistent.
        
        try:
            result = UNIFIED_OPS[op_name](text, rng)
        except Exception:
            # Fallback if an operator fails on weird input
            result = mutate_text_with_grammar(
                original_text=text, 
                grammar_spec=JSON_GRAMMAR if "json" in op_name else IP_GRAMMAR,
                rng=rng
            )
        
        # Record which operator produced this output
        if len(self._pending) >= _MAX_PENDING:
            self._pending.pop(next(iter(self._pending)))
        self._pending[result] = op_name
        
        return result

    def record_coverage(self, mutated_text: str, gained_coverage: bool) -> None:
        op_name = self._pending.pop(mutated_text, None)
        if op_name is not None:
            self.strategy.update_score(op_name, gained_coverage)

# Global instance for this version
_ALL_FUZZER = AdaptiveAllFuzzer()

def configure(grammar_file: str | None = None):
    """Configures the adaptive fuzzer with optional external grammar(s)."""
    if not grammar_file:
        return

    import os
    if os.path.isdir(grammar_file):
        # Load all JSON grammars in the directory
        for filename in os.listdir(grammar_file):
            if filename.endswith(".json"):
                path = os.path.join(grammar_file, filename)
                name = filename.split('.')[0]
                spec = load_grammar_from_json(path)
                _ALL_FUZZER.add_grammar_operator(name, spec)
    else:
        # Load a single grammar file
        name = os.path.basename(grammar_file).split('.')[0]
        spec = load_grammar_from_json(grammar_file)
        _ALL_FUZZER.add_grammar_operator(name, spec)

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
    # Only print if we actually did work
    proc_name = current_process().name
    strategy = _ALL_FUZZER.strategy
    total_usage = sum(strategy.usage.values())
    if total_usage < 10:
        return

    print("\n" + "="*60, flush=True)
    print(f" ADAPTIVE UNIFIED: FINAL OPERATOR PROBABILITIES ({proc_name})", flush=True)
    print(" (Running EVERYTHING: JSON + IP + Grammar + Havoc)", flush=True)
    print("="*60, flush=True)

    # Individual Operator Performance Table
    probs = strategy.get_probabilities()
    
    # Header
    print(f"\n[Operator Performance Report]", flush=True)
    print(f"  {'Operator':30s} | {'Usage':8s} | {'Success':8s} | {'Rate %':8s} | {'PSO Score':8s}", flush=True)
    print(f"  {'-'*30}-+-{'-'*8}-+-{'-'*8}-+-{'-'*8}-+-{'-'*8}", flush=True)
    
    # Sort by success first, then by current probability/score
    sorted_ops = sorted(
        UNIFIED_OPS.keys(), 
        key=lambda op: (strategy.success[op], probs[op]), 
        reverse=True
    )
    
    for op in sorted_ops:
        usage = strategy.usage[op]
        if usage == 0: continue
        
        success = strategy.success[op]
        rate = (success / usage * 100) if usage > 0 else 0
        score = probs[op]
        
        # Only show if it was used at least once or has some success
        print(f"  {op:30s} | {usage:8d} | {success:8d} | {rate:7.2f}% | {score:8.4f}", flush=True)
        
    print("="*60 + "\n", flush=True)

atexit.register(_print_final_probabilities)
