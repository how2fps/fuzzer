from __future__ import annotations

import random

from .grammar import generate_from_grammar, grammar_capabilities, normalize_grammar_spec
from .shared import (
    GrammarCapabilities,
    GrammarOperatorSpec,
    GrammarSpec,
    regenerate_text_without_nul,
    sanitize_mutated_text,
)
from .text_ops import (
    _alphabetic_label_substitution,
    _boundary_chars,
    _cross_branch_alternation_splice,
    _descending_range_surgery,
    _delimited_numeric_group_surgery,
    _duplicate_boundary_token,
    _embed_alternative_fragment,
    _extreme_numeric_surgery,
    _generic_invalidate_text,
    _insert_control_burst,
    _insert_control_char_in_quoted_span,
    _insert_foreign_punctuation,
    _insert_trailing_separator_before_closer,
    _mixed_separator_family_graft,
    _mutate_numeric_literal_in_text,
    _mutate_numeric_special_literal_in_text,
    _mutate_separator_char_in_text,
    _mutate_uppercase_literal_corruption,
    _mutate_text_from_original,
    _mutation_mode_probabilities,
    _neighbor_boundary_numeric_surgery,
    _numeric_format_surgery,
    _prefix_text_with_bom,
    _repetition_amplification_surgery,
    _remove_balanced_token,
    _replace_quoted_invalid_surrogate_escape,
    _replace_quoted_surrogate_pair_escape,
    _segment_count_change,
    _separator_confusion,
    _separator_run_surgery,
    _structured_range_surgery,
    _token_whitespace_surgery,
    _trailing_or_leading_extra_data,
    _validate_probability,
    _wildcard_suffix_surgery,
)

def _always_supported(_capabilities: GrammarCapabilities) -> bool:
    return True

def _supports_numeric_operator(capabilities: GrammarCapabilities) -> bool:
    return capabilities.has_numeric_literals or capabilities.has_number_ranges

def _supports_segment_count_operator(capabilities: GrammarCapabilities) -> bool:
    return capabilities.has_exact_parse_path and bool(capabilities.separator_chars)

def _supports_quoted_text_operator(capabilities: GrammarCapabilities) -> bool:
    return bool(capabilities.quote_chars)

def _supports_separator_operator(capabilities: GrammarCapabilities) -> bool:
    return bool(capabilities.separator_chars)

def _supports_extra_data_operator(capabilities: GrammarCapabilities) -> bool:
    return capabilities.has_exact_parse_path

def _supports_delimiter_invalidator(capabilities: GrammarCapabilities) -> bool:
    return (
        capabilities.has_delimiter_literals
        and bool(capabilities.paired_delimiters)
        and bool(capabilities.separator_chars)
    )

def _supports_boundary_operator(capabilities: GrammarCapabilities) -> bool:
    return bool(_boundary_chars(capabilities=capabilities))

def _supports_alternation_operator(capabilities: GrammarCapabilities) -> bool:
    return capabilities.has_alternation

def _supports_alternative_embed_operator(capabilities: GrammarCapabilities) -> bool:
    return capabilities.has_alternation and capabilities.has_delimiter_literals

def _supports_structured_range_operator(capabilities: GrammarCapabilities) -> bool:
    return (
        capabilities.has_numeric_literals
        and "-" in capabilities.literal_chars
        and bool(capabilities.separator_chars)
    )

def _supports_wildcard_suffix_operator(capabilities: GrammarCapabilities) -> bool:
    return "*" in capabilities.literal_chars and bool(capabilities.separator_chars)

def _supports_delimited_numeric_group_operator(
    capabilities: GrammarCapabilities,
) -> bool:
    return capabilities.has_numeric_literals and bool(capabilities.paired_delimiters)


def _supports_separator_run_operator(capabilities: GrammarCapabilities) -> bool:
    return bool(capabilities.separator_chars)


def _supports_token_whitespace_operator(capabilities: GrammarCapabilities) -> bool:
    return capabilities.has_numeric_literals or bool(
        _boundary_chars(capabilities=capabilities)
    )


def _supports_repetition_amplification_operator(
    capabilities: GrammarCapabilities,
) -> bool:
    return (
        bool(capabilities.separator_chars or capabilities.paired_delimiters)
        and (capabilities.has_repetition or capabilities.has_exact_parse_path)
    )


def _supports_alphabetic_label_operator(capabilities: GrammarCapabilities) -> bool:
    return bool(capabilities.separator_chars) and (
        capabilities.has_exact_parse_path or capabilities.has_alternation
    )


def _supports_mixed_separator_graft_operator(
    capabilities: GrammarCapabilities,
) -> bool:
    return capabilities.has_alternation and len(capabilities.separator_chars) >= 2


def _supports_neighbor_boundary_numeric_operator(
    capabilities: GrammarCapabilities,
) -> bool:
    return _supports_numeric_operator(capabilities)


def _supports_descending_range_operator(capabilities: GrammarCapabilities) -> bool:
    return capabilities.has_numeric_literals and "-" in capabilities.literal_chars

def _supported_grammar_operator_specs(
    *,
    capabilities: GrammarCapabilities,
) -> tuple[GrammarOperatorSpec, ...]:
    return tuple(
        spec for spec in _GRAMMAR_OPERATOR_SPECS if spec.supports(capabilities)
    )

def _pick_weighted_name(
    *,
    entries: list[tuple[str, float]],
    rng: random.Random,
) -> str:
    names = [name for name, _weight in entries]
    weights = [weight for _name, weight in entries]
    if hasattr(rng, "choices"):
        return rng.choices(names, weights=weights, k=1)[0]
    return rng.choice(names)

def _finalize_grammar_operator_candidate(
    *,
    original_text: str,
    fragment: str,
    candidate: str | None,
    rng: random.Random,
) -> str:
    if candidate and candidate != original_text:
        return sanitize_mutated_text(candidate)
    if not original_text:
        return sanitize_mutated_text(fragment)
    fallback = _mutate_text_from_original(
        original_text=original_text,
        fragment=fragment,
        rng=rng,
    )
    if fallback != original_text:
        return sanitize_mutated_text(fallback)
    if fragment != original_text:
        return sanitize_mutated_text(fragment)
    if len(original_text) > 1:
        return sanitize_mutated_text(original_text[:-1])
    return sanitize_mutated_text(original_text + "1")

def _apply_grammar_operator_with_context(
    *,
    operator_name: str,
    original_text: str,
    fragment: str,
    grammar_spec: GrammarSpec,
    max_depth: int,
    capabilities: GrammarCapabilities,
    rng: random.Random,
) -> str:
    if operator_name == "regenerate" or not original_text:
        return sanitize_mutated_text(fragment)
    spec = _GRAMMAR_OPERATOR_SPECS_BY_NAME.get(operator_name)
    if spec is None:
        raise ValueError(f"unknown grammar operator {operator_name!r}")
    candidate = None
    if spec.supports(capabilities):
        candidate = spec.apply(
            original_text,
            fragment,
            grammar_spec,
            max_depth,
            rng,
            capabilities,
        )
    return _finalize_grammar_operator_candidate(
        original_text=original_text,
        fragment=fragment,
        candidate=candidate,
        rng=rng,
    )

def available_grammar_operator_names(
    *,
    grammar_spec: GrammarSpec | dict[str, object],
) -> tuple[str, ...]:
    capabilities = grammar_capabilities(grammar_spec=grammar_spec)
    return (
        "regenerate",
        *(spec.name for spec in _supported_grammar_operator_specs(capabilities=capabilities)),
    )

def apply_grammar_operator(
    *,
    operator_name: str,
    original_text: str,
    grammar_spec: GrammarSpec,
    max_depth: int = 5,
    rng: random.Random | None = None,
) -> str:
    random_engine = rng or random.Random()
    normalized_spec = normalize_grammar_spec(grammar_spec=grammar_spec)
    capabilities = grammar_capabilities(grammar_spec=normalized_spec)
    def _produce() -> str:
        if operator_name == "regenerate" or not original_text:
            fragment = generate_from_grammar(
                grammar_spec=normalized_spec,
                max_depth=max_depth,
                rng=random_engine,
            )
            return _apply_grammar_operator_with_context(
                operator_name=operator_name,
                original_text=original_text,
                fragment=fragment,
                grammar_spec=normalized_spec,
                max_depth=max_depth,
                capabilities=capabilities,
                rng=random_engine,
            )

        spec = _GRAMMAR_OPERATOR_SPECS_BY_NAME.get(operator_name)
        if spec is None:
            raise ValueError(f"unknown grammar operator {operator_name!r}")
        fragment = ""
        if operator_name == "splice_fragment":
            fragment = generate_from_grammar(
                grammar_spec=normalized_spec,
                max_depth=max_depth,
                rng=random_engine,
            )
        candidate = None
        if spec.supports(capabilities):
            candidate = spec.apply(
                original_text,
                fragment,
                normalized_spec,
                max_depth,
                random_engine,
                capabilities,
            )
        if candidate and candidate != original_text:
            return sanitize_mutated_text(candidate)
        if not fragment:
            fragment = generate_from_grammar(
                grammar_spec=normalized_spec,
                max_depth=max_depth,
                rng=random_engine,
            )
        return _finalize_grammar_operator_candidate(
            original_text=original_text,
            fragment=fragment,
            candidate=candidate,
            rng=random_engine,
        )

    return regenerate_text_without_nul(_produce)

def mutate_text_with_grammar(
    *,
    original_text: str,
    grammar_spec: GrammarSpec,
    kind: str | None = None,
    max_depth: int = 5,
    regenerate_probability: float = 0.35,
    rng: random.Random | None = None,
) -> str:
    del kind
    random_engine = rng or random.Random()
    regenerate_probability = _validate_probability(
        name="regenerate_probability",
        value=regenerate_probability,
    )
    capabilities = grammar_capabilities(grammar_spec=grammar_spec)
    def _produce() -> str:
        if not original_text:
            return generate_from_grammar(
                grammar_spec=grammar_spec,
                max_depth=max_depth,
                rng=random_engine,
            )

        fragment = generate_from_grammar(
            grammar_spec=grammar_spec,
            max_depth=max_depth,
            rng=random_engine,
        )
        effective_regenerate_probability, invalid_probability = _mutation_mode_probabilities(
            original_text=original_text,
            capabilities=capabilities,
            base_regenerate_probability=regenerate_probability,
        )
        if random_engine.random() < effective_regenerate_probability:
            return _apply_grammar_operator_with_context(
                operator_name="regenerate",
                original_text=original_text,
                fragment=fragment,
                grammar_spec=grammar_spec,
                max_depth=max_depth,
                capabilities=capabilities,
                rng=random_engine,
            )

        mode = "invalid" if random_engine.random() < invalid_probability else "valid"
        entries = [
            (
                spec.name,
                spec.invalid_weight if mode == "invalid" else spec.valid_weight,
            )
            for spec in _supported_grammar_operator_specs(capabilities=capabilities)
            if (spec.invalid_weight if mode == "invalid" else spec.valid_weight) > 0.0
        ]
        operator_name = (
            _pick_weighted_name(entries=entries, rng=random_engine)
            if entries
            else "splice_fragment"
        )
        return _apply_grammar_operator_with_context(
            operator_name=operator_name,
            original_text=original_text,
            fragment=fragment,
            grammar_spec=grammar_spec,
            max_depth=max_depth,
            capabilities=capabilities,
            rng=random_engine,
        )

    return regenerate_text_without_nul(_produce)


_GRAMMAR_OPERATOR_SPECS: tuple[GrammarOperatorSpec, ...] = (
    GrammarOperatorSpec(
        name="splice_fragment",
        valid_weight=3.0,
        invalid_weight=2.0,
        supports=_always_supported,
        apply=lambda original_text, fragment, grammar_spec, max_depth, rng, capabilities: _mutate_text_from_original(
            original_text=original_text,
            fragment=fragment,
            rng=rng,
        ),
    ),
    GrammarOperatorSpec(
        name="mutate_numeric_literal",
        valid_weight=2.0,
        invalid_weight=1.5,
        supports=_supports_numeric_operator,
        apply=lambda original_text, fragment, grammar_spec, max_depth, rng, capabilities: _mutate_numeric_literal_in_text(
            text=original_text,
            rng=rng,
        ),
    ),
    GrammarOperatorSpec(
        name="mutate_numeric_special_literal",
        valid_weight=0.0,
        invalid_weight=1.4,
        supports=_supports_numeric_operator,
        apply=lambda original_text, fragment, grammar_spec, max_depth, rng, capabilities: _mutate_numeric_special_literal_in_text(
            text=original_text,
            rng=rng,
        ),
    ),
    GrammarOperatorSpec(
        name="numeric_format_surgery",
        valid_weight=0.4,
        invalid_weight=2.5,
        supports=_supports_numeric_operator,
        apply=lambda original_text, fragment, grammar_spec, max_depth, rng, capabilities: _numeric_format_surgery(
            text=original_text,
            capabilities=capabilities,
            rng=rng,
        ),
    ),
    GrammarOperatorSpec(
        name="extreme_numeric_surgery",
        valid_weight=0.0,
        invalid_weight=2.2,
        supports=_supports_numeric_operator,
        apply=lambda original_text, fragment, grammar_spec, max_depth, rng, capabilities: _extreme_numeric_surgery(
            text=original_text,
            rng=rng,
        ),
    ),
    GrammarOperatorSpec(
        name="neighbor_boundary_numeric_surgery",
        valid_weight=0.2,
        invalid_weight=2.6,
        supports=_supports_neighbor_boundary_numeric_operator,
        apply=lambda original_text, fragment, grammar_spec, max_depth, rng, capabilities: _neighbor_boundary_numeric_surgery(
            text=original_text,
            rng=rng,
        ),
    ),
    GrammarOperatorSpec(
        name="mutate_separator_char",
        valid_weight=1.5,
        invalid_weight=0.0,
        supports=_supports_separator_operator,
        apply=lambda original_text, fragment, grammar_spec, max_depth, rng, capabilities: _mutate_separator_char_in_text(
            text=original_text,
            rng=rng,
            capabilities=capabilities,
        ),
    ),
    GrammarOperatorSpec(
        name="separator_run_surgery",
        valid_weight=0.0,
        invalid_weight=3.0,
        supports=_supports_separator_run_operator,
        apply=lambda original_text, fragment, grammar_spec, max_depth, rng, capabilities: _separator_run_surgery(
            text=original_text,
            rng=rng,
            capabilities=capabilities,
        ),
    ),
    GrammarOperatorSpec(
        name="repetition_amplification_surgery",
        valid_weight=0.4,
        invalid_weight=2.0,
        supports=_supports_repetition_amplification_operator,
        apply=lambda original_text, fragment, grammar_spec, max_depth, rng, capabilities: _repetition_amplification_surgery(
            text=original_text,
            rng=rng,
            capabilities=capabilities,
        ),
    ),
    GrammarOperatorSpec(
        name="alphabetic_label_substitution",
        valid_weight=0.3,
        invalid_weight=0.6,
        supports=_supports_alphabetic_label_operator,
        apply=lambda original_text, fragment, grammar_spec, max_depth, rng, capabilities: _alphabetic_label_substitution(
            text=original_text,
            rng=rng,
            capabilities=capabilities,
        ),
    ),
    GrammarOperatorSpec(
        name="token_whitespace_surgery",
        valid_weight=0.0,
        invalid_weight=2.2,
        supports=_supports_token_whitespace_operator,
        apply=lambda original_text, fragment, grammar_spec, max_depth, rng, capabilities: _token_whitespace_surgery(
            text=original_text,
            rng=rng,
            capabilities=capabilities,
        ),
    ),
    GrammarOperatorSpec(
        name="mixed_separator_family_graft",
        valid_weight=0.6,
        invalid_weight=1.7,
        supports=_supports_mixed_separator_graft_operator,
        apply=lambda original_text, fragment, grammar_spec, max_depth, rng, capabilities: _mixed_separator_family_graft(
            original_text=original_text,
            grammar_spec=grammar_spec,
            max_depth=max_depth,
            rng=rng,
            capabilities=capabilities,
        ),
    ),
    GrammarOperatorSpec(
        name="segment_count_change",
        valid_weight=0.3,
        invalid_weight=1.8,
        supports=_supports_segment_count_operator,
        apply=lambda original_text, fragment, grammar_spec, max_depth, rng, capabilities: _segment_count_change(
            text=original_text,
            rng=rng,
            capabilities=capabilities,
        ),
    ),
    GrammarOperatorSpec(
        name="structured_range_surgery",
        valid_weight=0.9,
        invalid_weight=2.3,
        supports=_supports_structured_range_operator,
        apply=lambda original_text, fragment, grammar_spec, max_depth, rng, capabilities: _structured_range_surgery(
            text=original_text,
            rng=rng,
            capabilities=capabilities,
        ),
    ),
    GrammarOperatorSpec(
        name="descending_range_surgery",
        valid_weight=0.0,
        invalid_weight=2.0,
        supports=_supports_descending_range_operator,
        apply=lambda original_text, fragment, grammar_spec, max_depth, rng, capabilities: _descending_range_surgery(
            text=original_text,
            rng=rng,
            capabilities=capabilities,
        ),
    ),
    GrammarOperatorSpec(
        name="wildcard_suffix_surgery",
        valid_weight=0.8,
        invalid_weight=1.2,
        supports=_supports_wildcard_suffix_operator,
        apply=lambda original_text, fragment, grammar_spec, max_depth, rng, capabilities: _wildcard_suffix_surgery(
            text=original_text,
            rng=rng,
            capabilities=capabilities,
        ),
    ),
    GrammarOperatorSpec(
        name="delimited_numeric_group_surgery",
        valid_weight=0.8,
        invalid_weight=2.4,
        supports=_supports_delimited_numeric_group_operator,
        apply=lambda original_text, fragment, grammar_spec, max_depth, rng, capabilities: _delimited_numeric_group_surgery(
            text=original_text,
            rng=rng,
            capabilities=capabilities,
        ),
    ),
    GrammarOperatorSpec(
        name="separator_confusion",
        valid_weight=0.0,
        invalid_weight=2.1,
        supports=_supports_separator_operator,
        apply=lambda original_text, fragment, grammar_spec, max_depth, rng, capabilities: _separator_confusion(
            text=original_text,
            rng=rng,
            capabilities=capabilities,
        ),
    ),
    GrammarOperatorSpec(
        name="insert_foreign_punctuation",
        valid_weight=0.0,
        invalid_weight=2.5,
        supports=_always_supported,
        apply=lambda original_text, fragment, grammar_spec, max_depth, rng, capabilities: _insert_foreign_punctuation(
            text=original_text,
            rng=rng,
            capabilities=capabilities,
        ),
    ),
    GrammarOperatorSpec(
        name="uppercase_literal_corruption",
        valid_weight=0.2,
        invalid_weight=2.0,
        supports=_always_supported,
        apply=lambda original_text, fragment, grammar_spec, max_depth, rng, capabilities: _mutate_uppercase_literal_corruption(
            text=original_text,
            rng=rng,
            capabilities=capabilities,
        ),
    ),
    GrammarOperatorSpec(
        name="insert_control_burst",
        valid_weight=0.0,
        invalid_weight=2.5,
        supports=_always_supported,
        apply=lambda original_text, fragment, grammar_spec, max_depth, rng, capabilities: _insert_control_burst(
            text=original_text,
            rng=rng,
        ),
    ),
    GrammarOperatorSpec(
        name="insert_control_char_in_quoted_span",
        valid_weight=0.0,
        invalid_weight=3.0,
        supports=_supports_quoted_text_operator,
        apply=lambda original_text, fragment, grammar_spec, max_depth, rng, capabilities: _insert_control_char_in_quoted_span(
            text=original_text,
            rng=rng,
            capabilities=capabilities,
        ),
    ),
    GrammarOperatorSpec(
        name="replace_quoted_surrogate_pair_escape",
        valid_weight=0.9,
        invalid_weight=0.4,
        supports=_supports_quoted_text_operator,
        apply=lambda original_text, fragment, grammar_spec, max_depth, rng, capabilities: _replace_quoted_surrogate_pair_escape(
            text=original_text,
            rng=rng,
            capabilities=capabilities,
        ),
    ),
    GrammarOperatorSpec(
        name="replace_quoted_invalid_surrogate_escape",
        valid_weight=0.0,
        invalid_weight=1.0,
        supports=_supports_quoted_text_operator,
        apply=lambda original_text, fragment, grammar_spec, max_depth, rng, capabilities: _replace_quoted_invalid_surrogate_escape(
            text=original_text,
            rng=rng,
            capabilities=capabilities,
        ),
    ),
    GrammarOperatorSpec(
        name="insert_trailing_separator_before_closer",
        valid_weight=0.0,
        invalid_weight=2.5,
        supports=_supports_delimiter_invalidator,
        apply=lambda original_text, fragment, grammar_spec, max_depth, rng, capabilities: _insert_trailing_separator_before_closer(
            text=original_text,
            rng=rng,
            capabilities=capabilities,
        ),
    ),
    GrammarOperatorSpec(
        name="duplicate_boundary_token",
        valid_weight=0.0,
        invalid_weight=2.0,
        supports=_supports_boundary_operator,
        apply=lambda original_text, fragment, grammar_spec, max_depth, rng, capabilities: _duplicate_boundary_token(
            text=original_text,
            rng=rng,
            capabilities=capabilities,
        ),
    ),
    GrammarOperatorSpec(
        name="remove_balanced_token",
        valid_weight=0.0,
        invalid_weight=2.0,
        supports=_supports_boundary_operator,
        apply=lambda original_text, fragment, grammar_spec, max_depth, rng, capabilities: _remove_balanced_token(
            text=original_text,
            rng=rng,
            capabilities=capabilities,
        ),
    ),
    GrammarOperatorSpec(
        name="cross_branch_alternation_splice",
        valid_weight=0.8,
        invalid_weight=1.8,
        supports=_supports_alternation_operator,
        apply=lambda original_text, fragment, grammar_spec, max_depth, rng, capabilities: _cross_branch_alternation_splice(
            original_text=original_text,
            grammar_spec=grammar_spec,
            max_depth=max_depth,
            rng=rng,
        ),
    ),
    GrammarOperatorSpec(
        name="embed_alternative_fragment",
        valid_weight=0.7,
        invalid_weight=1.4,
        supports=_supports_alternative_embed_operator,
        apply=lambda original_text, fragment, grammar_spec, max_depth, rng, capabilities: _embed_alternative_fragment(
            original_text=original_text,
            grammar_spec=grammar_spec,
            max_depth=max_depth,
            rng=rng,
        ),
    ),
    GrammarOperatorSpec(
        name="trailing_or_leading_extra_data",
        valid_weight=0.0,
        invalid_weight=2.0,
        supports=_supports_extra_data_operator,
        apply=lambda original_text, fragment, grammar_spec, max_depth, rng, capabilities: _trailing_or_leading_extra_data(
            original_text=original_text,
            fragment=fragment,
            grammar_spec=grammar_spec,
            max_depth=max_depth,
            rng=rng,
            capabilities=capabilities,
        ),
    ),
    GrammarOperatorSpec(
        name="leading_bom",
        valid_weight=0.0,
        invalid_weight=1.0,
        supports=_always_supported,
        apply=lambda original_text, fragment, grammar_spec, max_depth, rng, capabilities: _prefix_text_with_bom(
            original_text=original_text
        ),
    ),
)
_GRAMMAR_OPERATOR_SPECS_BY_NAME = {
    spec.name: spec for spec in _GRAMMAR_OPERATOR_SPECS
}


__all__ = [
    "available_grammar_operator_names",
    "apply_grammar_operator",
    "mutate_text_with_grammar",
    "_mutation_mode_probabilities",
    "_generic_invalidate_text",
]
