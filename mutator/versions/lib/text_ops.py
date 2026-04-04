from __future__ import annotations

import random
import re

from .grammar import (
    _build_rule_graph,
    _expand_symbol_with_production_index,
    _reachable_rule_symbols,
    generate_from_grammar,
    normalize_grammar_spec,
)
from .shared import (
    GrammarCapabilities,
    GrammarSpec,
    _CONTROL_BURST_CHARS,
    _EXPONENT_SUFFIXES,
    _FOREIGN_PUNCTUATION_CHARS,
    _INVALID_SURROGATE_ESCAPE_PAYLOADS,
    _SPECIAL_NUMERIC_LITERALS,
    _SUPPORTED_DELIMITER_PAIRS,
    _SUPPORTED_QUOTE_CHARS,
    _SURROGATE_PAIR_ESCAPE_PAYLOADS,
    _TEXT_BOM_PREFIX,
    _TOKEN_SPLIT_PATTERN,
    _UPPERCASE_LITERAL_FALLBACK,
    sanitize_mutated_text,
)

def _validate_probability(*, name: str, value: float) -> float:
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be between 0.0 and 1.0")
    return value

def _mutate_text_from_original(
    *,
    original_text: str,
    fragment: str,
    rng: random.Random,
) -> str:
    if not original_text:
        return fragment

    strategy = rng.choice(("insert", "replace", "delete"))
    start = rng.randrange(len(original_text))
    end = rng.randrange(start + 1, len(original_text) + 1)

    if strategy == "insert":
        return original_text[:start] + fragment + original_text[start:]
    if strategy == "replace":
        return original_text[:start] + fragment + original_text[end:]
    if len(original_text) == 1:
        return original_text + fragment
    return original_text[:start] + original_text[end:]

def _find_quoted_ranges(
    *,
    text: str,
    quote_chars: frozenset[str] | None = None,
) -> list[tuple[int, int, str]]:
    ranges: list[tuple[int, int, str]] = []
    supported_quote_chars = quote_chars or _SUPPORTED_QUOTE_CHARS
    quote_char: str | None = None
    escaped = False
    start = -1
    for index, char in enumerate(text):
        if quote_char is None:
            if char in supported_quote_chars:
                quote_char = char
                escaped = False
                start = index
            continue
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == quote_char:
            ranges.append((start, index, quote_char))
            quote_char = None
            start = -1
    return ranges

def _insert_control_char_in_quoted_span(
    *,
    text: str,
    rng: random.Random,
    capabilities: GrammarCapabilities | None = None,
) -> str | None:
    quote_chars = capabilities.quote_chars if capabilities is not None else None
    quoted_ranges = _find_quoted_ranges(text=text, quote_chars=quote_chars)
    if not quoted_ranges:
        return None
    start, end, _quote_char = rng.choice(quoted_ranges)
    insert_at = rng.randrange(start + 1, end + 1)
    payload = rng.choice(("\n", "\r", "\t", "\x01", "\x1f"))
    return sanitize_mutated_text(text[:insert_at] + payload + text[insert_at:])

def _wrap_payload_in_quote(
    *,
    payload: str,
    rng: random.Random,
    capabilities: GrammarCapabilities | None = None,
) -> str:
    quote_chars = (
        tuple(sorted(capabilities.quote_chars))
        if capabilities is not None and capabilities.quote_chars
        else ('"',)
    )
    quote_char = quote_chars[0] if len(quote_chars) == 1 else rng.choice(quote_chars)
    return f"{quote_char}{payload}{quote_char}"

def _replace_quoted_span_content(
    *,
    text: str,
    replacement: str,
    rng: random.Random,
    capabilities: GrammarCapabilities | None = None,
) -> str | None:
    quote_chars = capabilities.quote_chars if capabilities is not None else None
    quoted_ranges = _find_quoted_ranges(text=text, quote_chars=quote_chars)
    if not quoted_ranges:
        return None
    start, end, _quote_char = rng.choice(quoted_ranges)
    return sanitize_mutated_text(text[: start + 1] + replacement + text[end:])

def _insert_trailing_separator_before_closer(
    *,
    text: str,
    rng: random.Random,
    capabilities: GrammarCapabilities | None = None,
) -> str | None:
    separator_chars = (
        capabilities.separator_chars
        if capabilities is not None and capabilities.separator_chars
        else frozenset({",", ";", ":"})
    )
    paired_delimiters = (
        capabilities.paired_delimiters
        if capabilities is not None and capabilities.paired_delimiters
        else _SUPPORTED_DELIMITER_PAIRS
    )
    default_separator = (
        ","
        if "," in separator_chars
        else next(iter(separator_chars), ",")
    )
    closers = {closer: default_separator for _opener, closer in paired_delimiters}
    candidates = [
        index
        for index, char in enumerate(text)
        if char in closers and index > 0 and text[index - 1] not in separator_chars
    ]
    if not candidates:
        return None
    index = rng.choice(candidates)
    separator = closers[text[index]]
    return sanitize_mutated_text(text[:index] + separator + text[index:])

def _boundary_chars(
    *,
    capabilities: GrammarCapabilities | None = None,
) -> frozenset[str]:
    if capabilities is None:
        return frozenset(r',;:|/\[]{}()<>._-"\'`')
    chars = set(capabilities.separator_chars)
    chars.update(capabilities.quote_chars)
    for opener, closer in capabilities.paired_delimiters:
        chars.add(opener)
        chars.add(closer)
    return frozenset(chars)

def _duplicate_boundary_token(
    *,
    text: str,
    rng: random.Random,
    capabilities: GrammarCapabilities | None = None,
) -> str | None:
    supported_boundary_chars = _boundary_chars(capabilities=capabilities)
    candidate_indexes = [
        index for index, char in enumerate(text) if char in supported_boundary_chars
    ]
    if not candidate_indexes:
        return None
    index = rng.choice(candidate_indexes)
    return sanitize_mutated_text(text[: index + 1] + text[index] + text[index + 1 :])

def _remove_balanced_token(
    *,
    text: str,
    rng: random.Random,
    capabilities: GrammarCapabilities | None = None,
) -> str | None:
    supported_boundary_chars = _boundary_chars(capabilities=capabilities)
    candidate_indexes = [
        index for index, char in enumerate(text) if char in supported_boundary_chars
    ]
    if not candidate_indexes:
        return None
    index = rng.choice(candidate_indexes)
    return sanitize_mutated_text(text[:index] + text[index + 1 :])

def _prefix_text_with_bom(*, original_text: str) -> str | None:
    if not original_text or original_text.startswith(_TEXT_BOM_PREFIX):
        return None
    return _TEXT_BOM_PREFIX + original_text

def _find_numeric_ranges(*, text: str) -> list[tuple[int, int]]:
    return [(match.start(), match.end()) for match in re.finditer(r"-?\d+", text)]

def _looks_numericish_token(token: str) -> bool:
    if not token:
        return False
    lowered = token.lower()
    if lowered in {"nan", "inf", "infinity"}:
        return True
    if any(char.isdigit() for char in token):
        return True
    return all(char in "abcdefABCDEF" for char in token) and len(token) <= 8

def _find_numericish_ranges(*, text: str) -> list[tuple[int, int]]:
    return [
        (match.start(), match.end())
        for match in re.finditer(r"[A-Za-z0-9]+", text)
        if _looks_numericish_token(match.group(0))
    ]

def _separator_char_candidates(
    *,
    text: str,
    capabilities: GrammarCapabilities,
    min_occurrences: int = 1,
) -> tuple[str, ...]:
    present = [
        separator
        for separator in sorted(capabilities.separator_chars)
        if text.count(separator) >= min_occurrences
    ]
    present.sort(key=lambda separator: text.count(separator), reverse=True)
    return tuple(present)

def _materialize_fragment(
    *,
    fragment: str,
    grammar_spec: GrammarSpec,
    max_depth: int,
    rng: random.Random,
) -> str:
    if fragment:
        return fragment
    return generate_from_grammar(
        grammar_spec=grammar_spec,
        max_depth=max_depth,
        rng=rng,
    )

def _separator_indexes(
    *,
    text: str,
    capabilities: GrammarCapabilities,
) -> list[int]:
    return [
        index
        for index, char in enumerate(text)
        if char in capabilities.separator_chars
    ]

def _mutate_numeric_literal_in_text(*, text: str, rng: random.Random) -> str | None:
    numeric_ranges = _find_numeric_ranges(text=text)
    if not numeric_ranges:
        return None
    start, end = rng.choice(numeric_ranges)
    token = text[start:end]
    if not token:
        return None
    strategy = rng.choice(("flip_digit", "duplicate_digit", "delete_digit", "offset"))
    if strategy == "offset":
        try:
            next_value = int(token) + rng.choice((-9, -1, 1, 9))
            replacement = str(next_value)
        except ValueError:
            replacement = token
    else:
        body = token[1:] if token.startswith("-") else token
        if not body:
            return None
        index = rng.randrange(len(body))
        if strategy == "flip_digit":
            replacement_body = body[:index] + str(rng.randrange(10)) + body[index + 1 :]
        elif strategy == "duplicate_digit":
            replacement_body = body[: index + 1] + body[index] + body[index + 1 :]
        else:
            if len(body) == 1:
                replacement_body = str((int(body) + 1) % 10)
            else:
                replacement_body = body[:index] + body[index + 1 :]
        replacement = (
            "-" + replacement_body
            if token.startswith("-") and replacement_body
            else replacement_body
        )
    candidate = text[:start] + replacement + text[end:]
    if candidate == text:
        return None
    return sanitize_mutated_text(candidate)

def _mutate_numeric_special_literal_in_text(
    *,
    text: str,
    rng: random.Random,
) -> str | None:
    literals = [
        literal for literal in _SPECIAL_NUMERIC_LITERALS if literal != text
    ] or list(_SPECIAL_NUMERIC_LITERALS)
    replacement = rng.choice(literals)

    numeric_ranges = _find_numeric_ranges(text=text)
    if numeric_ranges:
        start, end = rng.choice(numeric_ranges)
        candidate = text[:start] + replacement + text[end:]
        if candidate != text:
            return sanitize_mutated_text(candidate)

    if replacement != text:
        return sanitize_mutated_text(replacement)
    return None

def _segment_count_change(
    *,
    text: str,
    rng: random.Random,
    capabilities: GrammarCapabilities,
) -> str | None:
    separator_candidates = _separator_char_candidates(
        text=text,
        capabilities=capabilities,
        min_occurrences=1,
    )
    if not separator_candidates:
        return None
    separator = rng.choice(separator_candidates)
    parts = text.split(separator)
    if len(parts) < 2:
        return None

    strategy = rng.choice(("add", "remove"))
    non_empty_parts = [part for part in parts if part]
    if strategy == "add" and non_empty_parts:
        source_segment = rng.choice(non_empty_parts)
        insert_at = rng.randrange(len(parts) + 1)
        candidate_parts = list(parts)
        candidate_parts.insert(insert_at, source_segment)
        candidate = separator.join(candidate_parts)
        if candidate != text:
            return sanitize_mutated_text(candidate)

    removable_indexes = [
        index for index, part in enumerate(parts) if part or len(parts) > 2
    ]
    if strategy == "remove" and len(parts) > 1 and removable_indexes:
        remove_at = rng.choice(removable_indexes)
        candidate_parts = list(parts)
        candidate_parts.pop(remove_at)
        candidate = separator.join(candidate_parts)
        if candidate != text:
            return sanitize_mutated_text(candidate)

    return None

def _separator_confusion(
    *,
    text: str,
    rng: random.Random,
    capabilities: GrammarCapabilities,
) -> str | None:
    separator_positions = _separator_indexes(text=text, capabilities=capabilities)
    if not separator_positions:
        return None
    index = rng.choice(separator_positions)
    current = text[index]
    strategy = rng.choice(("delete", "duplicate", "replace", "transpose"))

    if strategy == "delete":
        candidate = text[:index] + text[index + 1 :]
    elif strategy == "duplicate":
        candidate = text[: index + 1] + current + text[index + 1 :]
    elif strategy == "replace":
        replacements = [
            separator
            for separator in capabilities.separator_chars
            if separator != current
        ]
        if not replacements:
            return None
        replacement = rng.choice(replacements)
        candidate = text[:index] + replacement + text[index + 1 :]
    else:
        swap_index: int | None = None
        if index + 1 < len(text) and text[index + 1] not in capabilities.separator_chars:
            swap_index = index + 1
        elif index > 0 and text[index - 1] not in capabilities.separator_chars:
            swap_index = index - 1
        if swap_index is None:
            return None
        chars = list(text)
        chars[index], chars[swap_index] = chars[swap_index], chars[index]
        candidate = "".join(chars)

    if candidate != text:
        return sanitize_mutated_text(candidate)
    return None

def _numeric_format_surgery(
    *,
    text: str,
    rng: random.Random,
) -> str | None:
    numeric_ranges = _find_numericish_ranges(text=text)
    if not numeric_ranges:
        return None
    start, end = rng.choice(numeric_ranges)
    token = text[start:end]
    if not token:
        return None

    strategy = rng.choice(
        (
            "leading_zero_pad",
            "toggle_sign",
            "insert_decimal",
            "remove_decimal",
            "add_exponent",
            "remove_exponent",
            "overflow_length",
        )
    )
    replacement = token

    if strategy == "leading_zero_pad":
        replacement = ("0" * rng.randint(1, 3)) + token
    elif strategy == "toggle_sign":
        if token.startswith(("+", "-")):
            replacement = token[1:] or token
        else:
            replacement = "-" + token
    elif strategy == "insert_decimal":
        if "." not in token and len(token) > 1:
            insert_at = rng.randrange(1, len(token))
            replacement = token[:insert_at] + "." + token[insert_at:]
    elif strategy == "remove_decimal":
        if "." in token:
            replacement = token.replace(".", "", 1)
    elif strategy == "add_exponent":
        if "e" not in token.lower():
            replacement = token + rng.choice(_EXPONENT_SUFFIXES)
    elif strategy == "remove_exponent":
        exponent_positions = [
            index for index, char in enumerate(token) if char in {"e", "E"}
        ]
        if exponent_positions:
            replacement = token[: exponent_positions[0]]
    elif strategy == "overflow_length":
        growth_char = token[-1] if token else "0"
        replacement = token + (growth_char * rng.randint(1, 3))

    if replacement == token:
        return None
    candidate = text[:start] + replacement + text[end:]
    if candidate != text:
        return sanitize_mutated_text(candidate)
    return None

def _mutate_quoted_escape_payload(
    *,
    text: str,
    payloads: tuple[str, ...],
    rng: random.Random,
    capabilities: GrammarCapabilities | None = None,
) -> str | None:
    payload = rng.choice(payloads)
    candidate = _replace_quoted_span_content(
        text=text,
        replacement=payload,
        rng=rng,
        capabilities=capabilities,
    )
    if candidate and candidate != text:
        return candidate

    wrapped = _wrap_payload_in_quote(
        payload=payload,
        rng=rng,
        capabilities=capabilities,
    )
    if wrapped != text:
        return sanitize_mutated_text(wrapped)
    return None

def _replace_quoted_surrogate_pair_escape(
    *,
    text: str,
    rng: random.Random,
    capabilities: GrammarCapabilities | None = None,
) -> str | None:
    return _mutate_quoted_escape_payload(
        text=text,
        payloads=_SURROGATE_PAIR_ESCAPE_PAYLOADS,
        rng=rng,
        capabilities=capabilities,
    )

def _replace_quoted_invalid_surrogate_escape(
    *,
    text: str,
    rng: random.Random,
    capabilities: GrammarCapabilities | None = None,
) -> str | None:
    return _mutate_quoted_escape_payload(
        text=text,
        payloads=_INVALID_SURROGATE_ESCAPE_PAYLOADS,
        rng=rng,
        capabilities=capabilities,
    )

def _mutate_separator_char_in_text(
    *,
    text: str,
    rng: random.Random,
    capabilities: GrammarCapabilities,
) -> str | None:
    if len(capabilities.separator_chars) < 2:
        return None
    candidate_indexes = [
        index for index, char in enumerate(text) if char in capabilities.separator_chars
    ]
    if not candidate_indexes:
        return None
    index = rng.choice(candidate_indexes)
    current = text[index]
    replacements = [
        separator for separator in capabilities.separator_chars if separator != current
    ]
    if not replacements:
        return None
    replacement = rng.choice(replacements)
    return sanitize_mutated_text(text[:index] + replacement + text[index + 1 :])

def _insert_foreign_punctuation(
    *,
    text: str,
    rng: random.Random,
    capabilities: GrammarCapabilities,
) -> str | None:
    punctuation_choices = tuple(
        char
        for char in _FOREIGN_PUNCTUATION_CHARS
        if char not in capabilities.literal_chars
    ) or _FOREIGN_PUNCTUATION_CHARS
    burst_length = rng.randint(1, 3)
    payload = "".join(rng.choice(punctuation_choices) for _ in range(burst_length))
    insert_at = rng.randrange(len(text) + 1) if text else 0
    return sanitize_mutated_text(text[:insert_at] + payload + text[insert_at:])

def _uppercase_literal_choices(
    *,
    capabilities: GrammarCapabilities,
) -> tuple[str, ...]:
    literal_choices = {
        char.upper()
        for char in capabilities.literal_chars
        if char.isalpha() and char.upper() != "\x00"
    }
    if literal_choices:
        return tuple(sorted(literal_choices))
    return _UPPERCASE_LITERAL_FALLBACK

def _mutate_uppercase_literal_corruption(
    *,
    text: str,
    rng: random.Random,
    capabilities: GrammarCapabilities,
) -> str | None:
    uppercase_choices = _uppercase_literal_choices(capabilities=capabilities)
    alpha_indexes = [index for index, char in enumerate(text) if char.isalpha()]
    if not alpha_indexes:
        insert_at = rng.randrange(len(text) + 1) if text else 0
        return sanitize_mutated_text(
            text[:insert_at] + rng.choice(uppercase_choices) + text[insert_at:]
        )

    index = rng.choice(alpha_indexes)
    strategy = rng.choice(("upper_char", "upper_token", "swap_upper", "duplicate_upper"))
    if strategy == "upper_token":
        start = index
        while start > 0 and text[start - 1].isalpha():
            start -= 1
        end = index + 1
        while end < len(text) and text[end].isalpha():
            end += 1
        replacement = text[start:end].upper()
        candidate = text[:start] + replacement + text[end:]
    elif strategy == "duplicate_upper":
        replacement = text[index].upper()
        candidate = text[: index + 1] + replacement + text[index + 1 :]
    else:
        replacement = (
            text[index].upper()
            if strategy == "upper_char"
            else rng.choice(uppercase_choices)
        )
        candidate = text[:index] + replacement + text[index + 1 :]

    if candidate == text:
        insert_at = rng.randrange(len(text) + 1)
        candidate = text[:insert_at] + rng.choice(uppercase_choices) + text[insert_at:]
    return sanitize_mutated_text(candidate)

def _insert_control_burst(
    *,
    text: str,
    rng: random.Random,
) -> str | None:
    burst_length = rng.randint(1, 4)
    payload = "".join(rng.choice(_CONTROL_BURST_CHARS) for _ in range(burst_length))
    if not text:
        return sanitize_mutated_text(payload)
    insert_at = rng.randrange(len(text) + 1)
    if rng.random() < 0.35:
        start = rng.randrange(len(text))
        end = min(len(text), start + burst_length)
        return sanitize_mutated_text(text[:start] + payload + text[end:])
    return sanitize_mutated_text(text[:insert_at] + payload + text[insert_at:])

def _trailing_or_leading_extra_data(
    *,
    original_text: str,
    fragment: str,
    grammar_spec: GrammarSpec,
    max_depth: int,
    rng: random.Random,
    capabilities: GrammarCapabilities,
) -> str | None:
    if not original_text:
        return None
    base_fragment = _materialize_fragment(
        fragment=fragment,
        grammar_spec=grammar_spec,
        max_depth=max_depth,
        rng=rng,
    )
    noise_chars = tuple(
        char
        for char in _FOREIGN_PUNCTUATION_CHARS
        if char not in capabilities.literal_chars
    ) or _FOREIGN_PUNCTUATION_CHARS
    noise = "".join(
        rng.choice(noise_chars) for _ in range(rng.randint(1, 3))
    )
    strategy = rng.choice(
        ("append_valid", "prepend_valid", "append_noise", "prepend_noise")
    )
    if strategy == "append_valid":
        candidate = original_text + base_fragment
    elif strategy == "prepend_valid":
        candidate = base_fragment + original_text
    elif strategy == "append_noise":
        candidate = original_text + noise
    else:
        candidate = noise + original_text
    if candidate != original_text:
        return sanitize_mutated_text(candidate)
    return None

def _alternation_symbols_from_grammar(
    *,
    grammar_spec: GrammarSpec,
) -> tuple[str, ...]:
    normalized = normalize_grammar_spec(grammar_spec=grammar_spec)
    rules = normalized["rules"]
    graph = _build_rule_graph(rules=rules)
    reachable_symbols = _reachable_rule_symbols(start=normalized["start"], graph=graph)
    symbols_to_scan = list(reachable_symbols or rules)
    if normalized["start"] in symbols_to_scan:
        symbols_to_scan.remove(normalized["start"])
        symbols_to_scan.insert(0, normalized["start"])
    return tuple(
        symbol
        for symbol in symbols_to_scan
        if len(rules.get(symbol, ())) > 1
    )

def _tokenize_for_alternation_splice(*, text: str) -> tuple[str, ...]:
    tokens = tuple(token for token in _TOKEN_SPLIT_PATTERN.findall(text) if token)
    if tokens:
        return tokens
    return (text,) if text else ()

def _boundary_signature_chars(*, text: str) -> frozenset[str]:
    return frozenset(char for char in text if not char.isalnum())

def _splice_alternation_samples(
    *,
    sample_a: str,
    sample_b: str,
    rng: random.Random,
) -> str | None:
    if not sample_a or not sample_b:
        return None
    tokens_a = _tokenize_for_alternation_splice(text=sample_a)
    tokens_b = _tokenize_for_alternation_splice(text=sample_b)
    if len(tokens_a) < 2 or len(tokens_b) < 2:
        return None
    unique_a = _boundary_signature_chars(text=sample_a) - _boundary_signature_chars(text=sample_b)
    unique_b = _boundary_signature_chars(text=sample_b) - _boundary_signature_chars(text=sample_a)
    prefix_counts = list(range(1, len(tokens_a)))
    suffix_starts = list(range(1, len(tokens_b)))
    for _ in range(12):
        prefix_count = rng.choice(prefix_counts)
        suffix_start = rng.choice(suffix_starts)
        candidate = "".join(tokens_a[:prefix_count] + tokens_b[suffix_start:])
        if candidate in {sample_a, sample_b} or not candidate:
            continue
        if unique_a and not any(char in candidate for char in unique_a):
            continue
        if unique_b and not any(char in candidate for char in unique_b):
            continue
        return sanitize_mutated_text(candidate)
    fallback = "".join(tokens_a[: len(tokens_a) // 2] + tokens_b[len(tokens_b) // 2 :])
    if fallback and fallback not in {sample_a, sample_b}:
        return sanitize_mutated_text(fallback)
    return None

def _cross_branch_alternation_splice(
    *,
    original_text: str,
    grammar_spec: GrammarSpec,
    max_depth: int,
    rng: random.Random,
) -> str | None:
    normalized = normalize_grammar_spec(grammar_spec=grammar_spec)
    rules = normalized["rules"]
    recursive_symbols = normalized["recursive_symbols"]
    alternation_symbols = _alternation_symbols_from_grammar(grammar_spec=normalized)
    if not alternation_symbols:
        return None
    symbol = rng.choice(alternation_symbols)
    productions = rules.get(symbol, [])
    if len(productions) < 2:
        return None
    first_index = rng.randrange(len(productions))
    remaining_indexes = [
        production_index
        for production_index in range(len(productions))
        if production_index != first_index
    ]
    second_index = rng.choice(remaining_indexes)
    sample_a = _expand_symbol_with_production_index(
        symbol=symbol,
        production_index=first_index,
        rules=rules,
        recursive_symbols=recursive_symbols,
        depth=0,
        max_depth=max_depth,
        rng=rng,
    )
    sample_b = _expand_symbol_with_production_index(
        symbol=symbol,
        production_index=second_index,
        rules=rules,
        recursive_symbols=recursive_symbols,
        depth=0,
        max_depth=max_depth,
        rng=rng,
    )
    hybrid = _splice_alternation_samples(sample_a=sample_a, sample_b=sample_b, rng=rng)
    if hybrid is None:
        return None
    if not original_text or rng.random() < 0.4:
        return sanitize_mutated_text(hybrid)
    return sanitize_mutated_text(
        _mutate_text_from_original(
            original_text=original_text,
            fragment=hybrid,
            rng=rng,
        )
    )

def _embed_alternative_fragment_sample(
    *,
    sample_a: str,
    sample_b: str,
    rng: random.Random,
) -> str | None:
    tokens_a = list(_tokenize_for_alternation_splice(text=sample_a))
    tokens_b = list(_tokenize_for_alternation_splice(text=sample_b))
    if not tokens_a or len(tokens_b) < 2:
        return None

    payload_start = rng.randrange(len(tokens_b))
    payload_end = rng.randrange(payload_start + 1, len(tokens_b) + 1)
    payload = tokens_b[payload_start:payload_end]
    if not payload:
        return None
    insert_at = rng.randrange(len(tokens_a) + 1)
    candidate = "".join(tokens_a[:insert_at] + payload + tokens_a[insert_at:])
    if candidate in {sample_a, sample_b} or not candidate:
        return None
    return sanitize_mutated_text(candidate)

def _embed_alternative_fragment(
    *,
    original_text: str,
    grammar_spec: GrammarSpec,
    max_depth: int,
    rng: random.Random,
) -> str | None:
    normalized = normalize_grammar_spec(grammar_spec=grammar_spec)
    rules = normalized["rules"]
    recursive_symbols = normalized["recursive_symbols"]
    alternation_symbols = _alternation_symbols_from_grammar(grammar_spec=normalized)
    if not alternation_symbols:
        return None
    symbol = rng.choice(alternation_symbols)
    productions = rules.get(symbol, [])
    if len(productions) < 2:
        return None
    first_index = rng.randrange(len(productions))
    remaining_indexes = [
        production_index
        for production_index in range(len(productions))
        if production_index != first_index
    ]
    if not remaining_indexes:
        return None
    second_index = rng.choice(remaining_indexes)
    sample_a = _expand_symbol_with_production_index(
        symbol=symbol,
        production_index=first_index,
        rules=rules,
        recursive_symbols=recursive_symbols,
        depth=0,
        max_depth=max_depth,
        rng=rng,
    )
    sample_b = _expand_symbol_with_production_index(
        symbol=symbol,
        production_index=second_index,
        rules=rules,
        recursive_symbols=recursive_symbols,
        depth=0,
        max_depth=max_depth,
        rng=rng,
    )
    base_text = original_text if original_text and rng.random() < 0.5 else sample_a
    return _embed_alternative_fragment_sample(
        sample_a=base_text,
        sample_b=sample_b,
        rng=rng,
    )

def _text_surface_match_ratio(
    *,
    text: str,
    capabilities: GrammarCapabilities,
) -> float:
    if not text:
        return 0.0
    supported_chars = set(capabilities.literal_chars)
    supported_chars.update(capabilities.separator_chars)
    supported_chars.update(capabilities.quote_chars)
    for opener, closer in capabilities.paired_delimiters:
        supported_chars.add(opener)
        supported_chars.add(closer)
    matched = sum(
        1
        for char in text
        if char.isalnum() or char in supported_chars or char.isspace()
    )
    return matched / float(len(text))

def _mutation_mode_probabilities(
    *,
    original_text: str,
    capabilities: GrammarCapabilities,
    base_regenerate_probability: float,
) -> tuple[float, float]:
    regenerate_probability = base_regenerate_probability
    invalid_probability = 0.36
    if (
        capabilities.has_delimiter_literals
        or capabilities.has_repetition
        or capabilities.has_alternation
    ):
        invalid_probability = 0.30
    if capabilities.has_exact_parse_path:
        regenerate_probability += 0.08
        invalid_probability -= 0.08
    if capabilities.has_alternation:
        regenerate_probability += 0.04
        invalid_probability -= 0.03
    if capabilities.has_repetition:
        regenerate_probability += 0.03
        invalid_probability -= 0.02
    if original_text:
        surface_match_ratio = _text_surface_match_ratio(
            text=original_text,
            capabilities=capabilities,
        )
        if surface_match_ratio >= 0.9:
            regenerate_probability += 0.06
            invalid_probability -= 0.06
        elif surface_match_ratio >= 0.75:
            regenerate_probability += 0.03
            invalid_probability -= 0.03
    regenerate_probability = max(0.0, min(regenerate_probability, 0.7))
    invalid_probability = max(0.12, min(invalid_probability, 0.55))
    return regenerate_probability, invalid_probability

def _pick_strategy(
    *,
    entries: list[tuple[str, callable, float]],
    rng: random.Random,
):
    if hasattr(rng, "choices"):
        names = [name for name, _callback, _weight in entries]
        weights = [weight for _name, _callback, weight in entries]
        chosen_name = rng.choices(names, weights=weights, k=1)[0]
        for name, callback, _weight in entries:
            if name == chosen_name:
                return callback
    return rng.choice([callback for _name, callback, _weight in entries])

def _generic_invalidate_text(
    *,
    original_text: str,
    fragment: str,
    rng: random.Random,
    capabilities: GrammarCapabilities | None = None,
) -> str:
    strategy_entries: list[tuple[str, callable, float]] = []
    if capabilities is None or capabilities.quote_chars:
        strategy_entries.append(
            (
                "insert_control_char_in_quoted_span",
                lambda: _insert_control_char_in_quoted_span(
                    text=original_text,
                    rng=rng,
                    capabilities=capabilities,
                ),
                3.0,
            )
        )
    if capabilities is None or (
        capabilities.has_delimiter_literals
        and capabilities.paired_delimiters
        and capabilities.separator_chars
    ):
        strategy_entries.append(
            (
                "insert_trailing_separator_before_closer",
                lambda: _insert_trailing_separator_before_closer(
                    text=original_text,
                    rng=rng,
                    capabilities=capabilities,
                ),
                2.5,
            )
        )
    if capabilities is None or _boundary_chars(capabilities=capabilities):
        strategy_entries.extend(
            (
                (
                    "duplicate_boundary_token",
                    lambda: _duplicate_boundary_token(
                        text=original_text,
                        rng=rng,
                        capabilities=capabilities,
                    ),
                    2.0,
                ),
                (
                    "remove_balanced_token",
                    lambda: _remove_balanced_token(
                        text=original_text,
                        rng=rng,
                        capabilities=capabilities,
                    ),
                    2.0,
                ),
            )
        )
    if capabilities is None:
        strategy_entries.extend(
            (
                (
                    "insert_foreign_punctuation",
                    lambda: _insert_foreign_punctuation(
                        text=original_text,
                        rng=rng,
                        capabilities=GrammarCapabilities(
                            literal_chars=frozenset(),
                            separator_chars=frozenset(),
                            quote_chars=frozenset(),
                            paired_delimiters=(),
                            has_numeric_literals=False,
                            has_number_ranges=False,
                            has_repetition=False,
                            has_alternation=False,
                            has_delimiter_literals=False,
                            has_exact_parse_path=False,
                            recursive_nonterminals=frozenset(),
                            has_recursive_nonterminals=False,
                            has_recursive_rules=False,
                        ),
                    ),
                    2.5,
                ),
                (
                    "uppercase_literal_corruption",
                    lambda: _mutate_uppercase_literal_corruption(
                        text=original_text,
                        rng=rng,
                        capabilities=GrammarCapabilities(
                            literal_chars=frozenset(),
                            separator_chars=frozenset(),
                            quote_chars=frozenset(),
                            paired_delimiters=(),
                            has_numeric_literals=False,
                            has_number_ranges=False,
                            has_repetition=False,
                            has_alternation=False,
                            has_delimiter_literals=False,
                            has_exact_parse_path=False,
                            recursive_nonterminals=frozenset(),
                            has_recursive_nonterminals=False,
                            has_recursive_rules=False,
                        ),
                    ),
                    2.0,
                ),
            )
        )
    else:
        strategy_entries.extend(
            (
                (
                    "insert_foreign_punctuation",
                    lambda: _insert_foreign_punctuation(
                        text=original_text,
                        rng=rng,
                        capabilities=capabilities,
                    ),
                    2.5,
                ),
                (
                    "uppercase_literal_corruption",
                    lambda: _mutate_uppercase_literal_corruption(
                        text=original_text,
                        rng=rng,
                        capabilities=capabilities,
                    ),
                    2.0,
                ),
            )
        )
    strategy_entries.append(
        (
            "insert_control_burst",
            lambda: _insert_control_burst(
                text=original_text,
                rng=rng,
            ),
            2.5,
        )
    )
    if capabilities is None or capabilities.has_numeric_literals or capabilities.has_number_ranges:
        strategy_entries.append(
            (
                "mutate_numeric_literal",
                lambda: _mutate_numeric_literal_in_text(text=original_text, rng=rng),
                1.5,
            )
        )
    strategy_entries.extend(
        (
            (
                "splice_fragment",
                lambda: _mutate_text_from_original(
                    original_text=original_text,
                    fragment=fragment,
                    rng=rng,
                ),
                2.0,
            ),
            ("leading_bom", lambda: _prefix_text_with_bom(original_text=original_text), 1.0),
        )
    )
    for _ in range(12):
        candidate = _pick_strategy(entries=strategy_entries, rng=rng)()
        if candidate and candidate != original_text:
            return sanitize_mutated_text(candidate)
    return sanitize_mutated_text(
        _mutate_text_from_original(
            original_text=original_text,
            fragment=fragment,
            rng=rng,
        )
    )

__all__ = [
    "_validate_probability",
    "_insert_control_char_in_quoted_span",
    "_insert_trailing_separator_before_closer",
    "_mutation_mode_probabilities",
    "_generic_invalidate_text",
    "_mutate_text_from_original",
    "_mutate_numeric_literal_in_text",
    "_mutate_numeric_special_literal_in_text",
    "_numeric_format_surgery",
    "_mutate_separator_char_in_text",
    "_segment_count_change",
    "_separator_confusion",
    "_replace_quoted_surrogate_pair_escape",
    "_replace_quoted_invalid_surrogate_escape",
    "_insert_foreign_punctuation",
    "_mutate_uppercase_literal_corruption",
    "_insert_control_burst",
    "_cross_branch_alternation_splice",
    "_embed_alternative_fragment",
    "_trailing_or_leading_extra_data",
    "_boundary_chars",
    "_duplicate_boundary_token",
    "_remove_balanced_token",
    "_prefix_text_with_bom",
]
