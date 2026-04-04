from __future__ import annotations

import random
from pathlib import Path

from .grammar import generate_from_grammar, resolve_grammar_spec
from .operators import mutate_text_with_grammar

VALID_OUTPUT_PROBABILITY = 0.7


def _generate_input(
    *,
    kind: str,
    max_depth: int,
    rng: random.Random | None = None,
    grammar_path: str | Path | None = None,
) -> str:
    return generate_from_grammar(
        grammar_spec=resolve_grammar_spec(kind=kind, grammar_path=grammar_path),
        max_depth=max_depth,
        rng=rng,
    )


def generate_json_input(
    *,
    max_depth: int = 6,
    rng: random.Random | None = None,
    grammar_path: str | Path | None = None,
) -> str:
    return _generate_input(
        kind="json",
        max_depth=max_depth,
        rng=rng,
        grammar_path=grammar_path,
    )


def generate_ip_input(
    *,
    max_depth: int = 3,
    rng: random.Random | None = None,
    grammar_path: str | Path | None = None,
) -> str:
    return _generate_input(
        kind="ip",
        max_depth=max_depth,
        rng=rng,
        grammar_path=grammar_path,
    )


def generate_ipv4_input(
    *,
    max_depth: int = 2,
    rng: random.Random | None = None,
    grammar_path: str | Path | None = None,
) -> str:
    return _generate_input(
        kind="ipv4",
        max_depth=max_depth,
        rng=rng,
        grammar_path=grammar_path,
    )


def generate_ipv6_input(
    *,
    max_depth: int = 2,
    rng: random.Random | None = None,
    grammar_path: str | Path | None = None,
) -> str:
    return _generate_input(
        kind="ipv6",
        max_depth=max_depth,
        rng=rng,
        grammar_path=grammar_path,
    )


def _mutate_input(
    *,
    kind: str,
    original_text: str = "",
    max_depth: int,
    regenerate_probability: float = 0.35,
    rng: random.Random | None = None,
    grammar_path: str | Path | None = None,
) -> str:
    return mutate_text_with_grammar(
        original_text=original_text,
        grammar_spec=resolve_grammar_spec(kind=kind, grammar_path=grammar_path),
        kind=kind,
        max_depth=max_depth,
        regenerate_probability=regenerate_probability,
        rng=rng,
    )


def mutate_json_input(
    *,
    original_text: str = "",
    max_depth: int = 6,
    regenerate_probability: float = 0.35,
    rng: random.Random | None = None,
    grammar_path: str | Path | None = None,
) -> str:
    return _mutate_input(
        kind="json",
        original_text=original_text,
        max_depth=max_depth,
        regenerate_probability=regenerate_probability,
        rng=rng,
        grammar_path=grammar_path,
    )


def mutate_ip_input(
    *,
    original_text: str = "",
    max_depth: int = 3,
    regenerate_probability: float = 0.35,
    rng: random.Random | None = None,
    grammar_path: str | Path | None = None,
) -> str:
    return _mutate_input(
        kind="ip",
        original_text=original_text,
        max_depth=max_depth,
        regenerate_probability=regenerate_probability,
        rng=rng,
        grammar_path=grammar_path,
    )


def mutate_ipv4_input(
    *,
    original_text: str = "",
    max_depth: int = 2,
    regenerate_probability: float = 0.35,
    rng: random.Random | None = None,
    grammar_path: str | Path | None = None,
) -> str:
    return _mutate_input(
        kind="ipv4",
        original_text=original_text,
        max_depth=max_depth,
        regenerate_probability=regenerate_probability,
        rng=rng,
        grammar_path=grammar_path,
    )


def mutate_ipv6_input(
    *,
    original_text: str = "",
    max_depth: int = 2,
    regenerate_probability: float = 0.35,
    rng: random.Random | None = None,
    grammar_path: str | Path | None = None,
) -> str:
    return _mutate_input(
        kind="ipv6",
        original_text=original_text,
        max_depth=max_depth,
        regenerate_probability=regenerate_probability,
        rng=rng,
        grammar_path=grammar_path,
    )


__all__ = [
    "VALID_OUTPUT_PROBABILITY",
    "generate_json_input",
    "generate_ip_input",
    "generate_ipv4_input",
    "generate_ipv6_input",
    "mutate_json_input",
    "mutate_ip_input",
    "mutate_ipv4_input",
    "mutate_ipv6_input",
]
