"""
Base mutator: grammar-driven mutation using the active runtime grammar.
"""
from __future__ import annotations

import random

from .lib import mutate_text_with_grammar, resolve_grammar_spec


_DEFAULT_MAX_DEPTH = 5


def mutate(
    text: str,
    *,
    mutator_kind: str,
    rng: random.Random,
) -> str:
    """Mutate seed text with the configured grammar for this target family."""
    grammar_spec = resolve_grammar_spec(kind=mutator_kind)
    return mutate_text_with_grammar(
        original_text=text,
        grammar_spec=grammar_spec,
        max_depth=_DEFAULT_MAX_DEPTH,
        rng=rng,
    )
