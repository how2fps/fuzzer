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

_ULTRA_LONG_NUMERIC_MIN_DIGITS = 4301
_ULTRA_LONG_NUMERIC_MAX_DIGITS = 6000

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


def _structural_boundary_chars(
    *,
    capabilities: GrammarCapabilities | None = None,
) -> frozenset[str]:
    if capabilities is None:
        return frozenset("[]{}()<>\"'`")
    chars = set(capabilities.quote_chars)
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
    supported_boundary_chars = _structural_boundary_chars(capabilities=capabilities)
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
    supported_boundary_chars = _structural_boundary_chars(capabilities=capabilities)
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


def _find_alnum_token_ranges(
    *,
    text: str,
    min_length: int = 1,
) -> list[tuple[int, int]]:
    return [
        (match.start(), match.end())
        for match in re.finditer(r"[A-Za-z0-9]+", text)
        if (match.end() - match.start()) >= min_length
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


def _non_alnum_char_candidates(
    *,
    text: str,
    capabilities: GrammarCapabilities,
    min_occurrences: int = 1,
) -> tuple[str, ...]:
    return tuple(
        char
        for char in sorted(capabilities.non_alnum_chars)
        if text.count(char) >= min_occurrences
    )

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


def _duplicate_separator_once(
    *,
    text: str,
    rng: random.Random,
    capabilities: GrammarCapabilities,
) -> str | None:
    separator_positions = _separator_indexes(text=text, capabilities=capabilities)
    if not separator_positions:
        return None
    undoubled_positions = [
        index
        for index in separator_positions
        if index + 1 >= len(text) or text[index + 1] != text[index]
    ]
    candidate_positions = undoubled_positions or separator_positions
    index = rng.choice(candidate_positions)
    return _replace_text_range(
        text=text,
        start=index,
        end=index + 1,
        replacement=text[index] * 2,
    )

def _replace_text_range(
    *,
    text: str,
    start: int,
    end: int,
    replacement: str,
) -> str | None:
    candidate = text[:start] + replacement + text[end:]
    if candidate == text:
        return None
    return sanitize_mutated_text(candidate)


def _observed_numeric_values(*, text: str) -> tuple[list[int], list[int]]:
    values: list[int] = []
    widths: list[int] = []
    for start, end in _find_numeric_ranges(text=text):
        token = text[start:end]
        body = token.lstrip("+-")
        if not body or not body.isdigit():
            continue
        widths.append(len(body))
        try:
            values.append(int(token))
        except ValueError:
            continue
    return values, widths


def _random_alpha_label(
    *,
    rng: random.Random,
    min_length: int = 3,
    max_length: int = 6,
) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyz"
    length = rng.randint(min_length, max_length)
    return "".join(rng.choice(alphabet) for _ in range(length))


def _fallback_alpha_label(*, length: int) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyz"
    repeats = max(1, (length + len(alphabet) - 1) // len(alphabet))
    return (alphabet * repeats)[:length]


def _repeat_text_to_length(*, seed: str, length: int, filler: str = "0") -> str:
    base = seed or filler
    repeats = max(1, (length + len(base) - 1) // len(base))
    return (base * repeats)[:length]

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


def _zero_pad_numeric_token(
    *,
    text: str,
    rng: random.Random,
) -> str | None:
    numeric_ranges = _find_numeric_ranges(text=text)
    if not numeric_ranges:
        return None
    start, end = rng.choice(numeric_ranges)
    token = text[start:end]
    sign = token[0] if token.startswith(("+", "-")) else ""
    body = token[len(sign):]
    if not body or not body.isdigit():
        return None

    _values, widths = _observed_numeric_values(text=text)
    target_width = max(3, max(widths, default=0))
    if len(body) >= target_width:
        target_width = min(max(len(body) + 1, 3), len(body) + 3)
    replacement_body = body.zfill(target_width)
    if replacement_body == body:
        return None
    return _replace_text_range(
        text=text,
        start=start,
        end=end,
        replacement=sign + replacement_body,
    )

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
    strategy = rng.choice(("delete", "replace", "transpose"))

    if strategy == "delete":
        candidate = text[:index] + text[index + 1 :]
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


def _prefer_strategies(
    *,
    strategies: tuple[str, ...],
    preferred: tuple[str, ...],
) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for strategy in preferred + strategies:
        if strategy in strategies and strategy not in seen:
            ordered.append(strategy)
            seen.add(strategy)
    return tuple(ordered)


def _surrounding_delimiter_pair(
    *,
    text: str,
    start: int,
    end: int,
    capabilities: GrammarCapabilities,
) -> tuple[str, str] | None:
    for opener, closer in capabilities.paired_delimiters:
        if start > 0 and end < len(text) and text[start - 1] == opener and text[end] == closer:
            return opener, closer
    return None


def _separator_run_surgery(
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
    strategies = (
        "fanout",
        "neighbor_mix",
        "spam_run",
        "alternating_run",
    )
    if current == "/":
        strategies = _prefer_strategies(
            strategies=strategies,
            preferred=("fanout", "spam_run", "alternating_run"),
        )
    elif current == "-":
        strategies = _prefer_strategies(
            strategies=strategies,
            preferred=("fanout", "alternating_run", "spam_run"),
        )
    strategy = rng.choice(strategies)

    if strategy == "fanout":
        replacement = current * rng.randint(3, 4)
    elif strategy == "spam_run":
        replacement = current * rng.randint(5, 8)
    else:
        replacements = [
            separator
            for separator in capabilities.separator_chars
            if separator != current
        ]
        if not replacements:
            if strategy == "alternating_run":
                replacement = current * rng.randint(5, 8)
            else:
                return None
        else:
            other = rng.choice(replacements)
            if strategy == "alternating_run":
                unit = current + other if rng.choice(("suffix", "prefix")) == "suffix" else other + current
                replacement = unit * rng.randint(2, 4)
            else:
                join_order = rng.choice(("suffix", "prefix"))
                replacement = current + other if join_order == "suffix" else other + current

    return _replace_text_range(
        text=text,
        start=index,
        end=index + 1,
        replacement=replacement,
    )


def _separator_chars_in_text(
    *,
    text: str,
    capabilities: GrammarCapabilities,
) -> frozenset[str]:
    return frozenset(char for char in text if char in capabilities.separator_chars)


def _find_delimited_content_ranges(
    *,
    text: str,
    capabilities: GrammarCapabilities,
) -> list[tuple[int, int, str, str]]:
    ranges: list[tuple[int, int, str, str]] = []
    for opener, closer in capabilities.paired_delimiters:
        depth = 0
        content_start: int | None = None
        for index, char in enumerate(text):
            if char == opener:
                depth += 1
                if depth == 1:
                    content_start = index + 1
                continue
            if char == closer and depth > 0:
                if depth == 1 and content_start is not None and content_start < index:
                    ranges.append((content_start, index, opener, closer))
                    content_start = None
                depth -= 1
    return ranges


def _repetition_amplification_surgery(
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
    strategies = ["repeat_segment", "append_segment", "flood_segment"]
    delimited_ranges = _find_delimited_content_ranges(
        text=text,
        capabilities=capabilities,
    )
    if delimited_ranges:
        strategies.extend(("amplify_delimited_group", "flood_delimited_group"))
    strategy = rng.choice(tuple(strategies))

    if strategy in {"amplify_delimited_group", "flood_delimited_group"} and delimited_ranges:
        inner_start, inner_end, _opener, _closer = rng.choice(delimited_ranges)
        inner = text[inner_start:inner_end]
        if not inner:
            return None
        spam_repeat_count = (
            rng.randint(6, 12)
            if strategy == "flood_delimited_group"
            else rng.randint(3, 6)
        )
        if "-" in inner:
            pieces = [piece for piece in inner.split("-") if piece]
            if pieces:
                repeated_piece = rng.choice(pieces)
                replacement = inner + ("-" + repeated_piece) * (spam_repeat_count - 1)
                return _replace_text_range(
                    text=text,
                    start=inner_start,
                    end=inner_end,
                    replacement=replacement,
                )
        inner_separator_candidates = [
            separator for separator in separator_candidates if separator in inner
        ]
        if inner_separator_candidates:
            separator = rng.choice(inner_separator_candidates)
            pieces = [piece for piece in inner.split(separator) if piece]
            if pieces:
                repeated_piece = rng.choice(pieces)
                replacement = inner + (separator + repeated_piece) * (spam_repeat_count - 1)
                return _replace_text_range(
                    text=text,
                    start=inner_start,
                    end=inner_end,
                    replacement=replacement,
                )

    if not separator_candidates:
        return None
    separator = rng.choice(separator_candidates)
    parts = text.split(separator)
    non_empty_parts = [(index, part) for index, part in enumerate(parts) if part]
    if not non_empty_parts:
        return None
    part_index, part = rng.choice(non_empty_parts)
    repeat_count = rng.randint(6, 12) if strategy == "flood_segment" else rng.randint(3, 6)
    candidate_parts = list(parts)
    if strategy == "repeat_segment":
        candidate_parts[part_index] = separator.join([part] * repeat_count)
    else:
        candidate_parts[part_index : part_index + 1] = [part] * repeat_count
    candidate = separator.join(candidate_parts)
    if candidate != text:
        return sanitize_mutated_text(candidate)
    return None

def _structured_range_surgery(
    *,
    text: str,
    rng: random.Random,
    capabilities: GrammarCapabilities,
) -> str | None:
    if "-" not in capabilities.literal_chars:
        return None
    numeric_ranges = _find_numeric_ranges(text=text)
    if not numeric_ranges:
        return None
    start, end = rng.choice(numeric_ranges)
    token = text[start:end]
    if not token:
        return None
    separator_candidates = _separator_char_candidates(
        text=text,
        capabilities=capabilities,
        min_occurrences=1,
    )
    strategies = (
        "duplicate",
        "bounded_suffix",
        "segment_pair",
        "drop_left_endpoint_digit",
        "range_fanout",
    )
    surrounding_pair = _surrounding_delimiter_pair(
        text=text,
        start=start,
        end=end,
        capabilities=capabilities,
    )
    if surrounding_pair is not None:
        strategies = _prefer_strategies(
            strategies=strategies,
            preferred=("range_fanout", "bounded_suffix", "duplicate"),
        )
    elif "-" in text:
        strategies = _prefer_strategies(
            strategies=strategies,
            preferred=("bounded_suffix", "range_fanout", "drop_left_endpoint_digit"),
        )
    if token.lstrip("-").isdigit() and int(token) in {254, 255}:
        strategies = _prefer_strategies(
            strategies=strategies,
            preferred=("bounded_suffix", "range_fanout"),
        )
    strategy = rng.choice(strategies)
    if strategy == "duplicate":
        return _replace_text_range(
            text=text,
            start=start,
            end=end,
            replacement=f"{token}-{token}",
        )
    if strategy == "range_fanout":
        if not token.lstrip("-").isdigit():
            return None
        repeat_count = rng.randint(3, 8)
        replacement = token + ("-" + token) * (repeat_count - 1)
        return _replace_text_range(
            text=text,
            start=start,
            end=end,
            replacement=replacement,
        )
    if strategy == "bounded_suffix":
        if not token.lstrip("-").isdigit():
            return None
        base_value = int(token)
        if base_value in {254, 255}:
            offset = 1
        else:
            offset = rng.choice((1, 2, 5, 9))
        return _replace_text_range(
            text=text,
            start=start,
            end=end,
            replacement=f"{token}-{base_value + offset}",
        )
    if strategy == "drop_left_endpoint_digit":
        hyphen_indexes = [
            index
            for index, char in enumerate(text)
            if char == "-" and index > 0 and text[index - 1].isdigit()
        ]
        if not hyphen_indexes:
            return None
        hyphen_index = rng.choice(hyphen_indexes)
        candidate = text[: hyphen_index - 1] + text[hyphen_index:]
        if candidate != text:
            return sanitize_mutated_text(candidate)
        return None
    if not separator_candidates:
        return None
    separator = rng.choice(separator_candidates)
    parts = [part for part in text.split(separator) if part]
    if len(parts) < 2:
        return None
    source = rng.choice(parts)
    peer_choices = [part for part in parts if part != source] or parts
    peer = rng.choice(peer_choices)
    if source == peer:
        return None
    return _replace_text_range(
        text=text,
        start=start,
        end=end,
        replacement=f"{source}-{peer}",
    )


def _numeric_token_bounds_adjacent_to_hyphen(
    *,
    text: str,
    hyphen_index: int,
) -> tuple[tuple[int, int], tuple[int, int]] | None:
    left_end = hyphen_index
    left_start = left_end
    while left_start > 0 and text[left_start - 1].isdigit():
        left_start -= 1
    if left_start == left_end:
        return None
    if (
        left_start > 0
        and text[left_start - 1] == "-"
        and (left_start - 1 == 0 or not text[left_start - 2].isdigit())
    ):
        left_start -= 1

    right_start = hyphen_index + 1
    if right_start >= len(text):
        return None
    if (
        text[right_start] in "+-"
        and right_start + 1 < len(text)
        and text[right_start + 1].isdigit()
    ):
        right_end = right_start + 2
    elif text[right_start].isdigit():
        right_end = right_start + 1
    else:
        return None
    while right_end < len(text) and text[right_end].isdigit():
        right_end += 1

    return (left_start, left_end), (right_start, right_end)


def _descending_range_surgery(
    *,
    text: str,
    rng: random.Random,
    capabilities: GrammarCapabilities,
) -> str | None:
    if "-" not in capabilities.literal_chars:
        return None

    strategy = rng.choice(("reverse_existing", "synthesize_descending"))
    hyphen_indexes = [index for index, char in enumerate(text) if char == "-"]

    if strategy == "reverse_existing":
        pair_candidates = [
            (hyphen_index, bounds)
            for hyphen_index in hyphen_indexes
            if (bounds := _numeric_token_bounds_adjacent_to_hyphen(
                text=text,
                hyphen_index=hyphen_index,
            ))
            is not None
        ]
        if pair_candidates:
            _hyphen_index, ((left_start, left_end), (right_start, right_end)) = rng.choice(
                pair_candidates
            )
            left_token = text[left_start:left_end]
            right_token = text[right_start:right_end]
            try:
                left_value = int(left_token)
                right_value = int(right_token)
            except ValueError:
                return None
            if left_value == right_value:
                higher = left_value + max(1, rng.randint(1, 9))
                lower = left_value
            else:
                higher = max(left_value, right_value)
                lower = min(left_value, right_value)
            return _replace_text_range(
                text=text,
                start=left_start,
                end=right_end,
                replacement=f"{higher}-{lower}",
            )

    numeric_ranges = _find_numeric_ranges(text=text)
    if not numeric_ranges:
        return None
    start, end = rng.choice(numeric_ranges)
    token = text[start:end]
    try:
        base_value = int(token)
    except ValueError:
        return None
    offset = rng.choice((1, 2, 5, 9))
    higher = base_value + offset
    return _replace_text_range(
        text=text,
        start=start,
        end=end,
        replacement=f"{higher}-{base_value}",
    )

def _non_alnum_run_surgery(
    *,
    text: str,
    rng: random.Random,
    capabilities: GrammarCapabilities,
) -> str | None:
    symbol_candidates = _non_alnum_char_candidates(
        text=text,
        capabilities=capabilities,
        min_occurrences=1,
    )
    if not symbol_candidates:
        return None
    symbol = rng.choice(symbol_candidates)
    positions = [index for index, char in enumerate(text) if char == symbol]
    if not positions:
        return None
    index = rng.choice(positions)
    repeat_count = rng.randint(2, 10)
    return _replace_text_range(
        text=text,
        start=index,
        end=index + 1,
        replacement=symbol * repeat_count,
    )

def _delimited_numeric_group_surgery(
    *,
    text: str,
    rng: random.Random,
    capabilities: GrammarCapabilities,
) -> str | None:
    if not capabilities.paired_delimiters:
        return None
    numeric_ranges = _find_numeric_ranges(text=text)
    if not numeric_ranges:
        return None
    delimited_numeric_ranges = [
        range_
        for range_ in numeric_ranges
        if _surrounding_delimiter_pair(
            text=text,
            start=range_[0],
            end=range_[1],
            capabilities=capabilities,
        )
        is not None
    ]
    if delimited_numeric_ranges:
        numeric_ranges = delimited_numeric_ranges + [
            range_ for range_ in numeric_ranges if range_ not in delimited_numeric_ranges
        ]
    start, end = rng.choice(numeric_ranges)
    token = text[start:end]
    if not token:
        return None
    surrounding_pair = _surrounding_delimiter_pair(
        text=text,
        start=start,
        end=end,
        capabilities=capabilities,
    )
    if surrounding_pair is not None:
        opener, closer = surrounding_pair
        strategies = ("range_group", "spam_group")
    else:
        opener, closer = rng.choice(capabilities.paired_delimiters)
        strategies = ("wrap_group", "range_group", "spam_group")
    if surrounding_pair is not None:
        strategies = _prefer_strategies(
            strategies=strategies,
            preferred=("range_group", "spam_group"),
        )
    elif "-" in text:
        strategies = _prefer_strategies(
            strategies=strategies,
            preferred=("range_group", "spam_group", "wrap_group"),
        )
    strategy = rng.choice(strategies)
    inner = token
    if strategy != "wrap_group" and "-" in capabilities.literal_chars and token.lstrip("-").isdigit():
        offset = rng.choice((1, 2, 5, 9))
        if strategy == "spam_group":
            repeated = str(int(token) + offset)
            repeat_count = rng.randint(3, 8)
            inner = token + ("-" + repeated) * (repeat_count - 1)
        elif strategy == "range_group":
            inner = f"{token}-{int(token) + offset}"
    replacement = inner if surrounding_pair is not None else f"{opener}{inner}{closer}"
    return _replace_text_range(
        text=text,
        start=start,
        end=end,
        replacement=replacement,
    )

def _numeric_format_surgery(
    *,
    text: str,
    capabilities: GrammarCapabilities | None = None,
    rng: random.Random,
) -> str | None:
    numeric_ranges = _find_numericish_ranges(text=text)
    if not numeric_ranges:
        return None
    slash_ranges = [
        range_ for range_ in numeric_ranges if range_[0] > 0 and text[range_[0] - 1] == "/"
    ]
    delimited_ranges: list[tuple[int, int]] = []
    if capabilities is not None:
        delimited_ranges = [
            range_
            for range_ in numeric_ranges
            if _surrounding_delimiter_pair(
                text=text,
                start=range_[0],
                end=range_[1],
                capabilities=capabilities,
            )
            is not None
        ]
    if slash_ranges:
        numeric_ranges = slash_ranges + [range_ for range_ in numeric_ranges if range_ not in slash_ranges]
    elif delimited_ranges:
        numeric_ranges = delimited_ranges + [
            range_ for range_ in numeric_ranges if range_ not in delimited_ranges
        ]
    start, end = rng.choice(numeric_ranges)
    token = text[start:end]
    if not token:
        return None
    slash_mask = start > 0 and text[start - 1] == "/"
    surrounding_pair = (
        _surrounding_delimiter_pair(
            text=text,
            start=start,
            end=end,
            capabilities=capabilities,
        )
        if capabilities is not None
        else None
    )
    strategies = (
        "heavy_zero_pad",
        "toggle_sign",
        "insert_decimal",
        "remove_decimal",
        "add_exponent",
        "remove_exponent",
        "overflow_length",
        "tail_digit_burst",
    )
    if slash_mask:
        strategies = _prefer_strategies(
            strategies=strategies,
            preferred=("tail_digit_burst", "overflow_length", "heavy_zero_pad"),
        )
    elif surrounding_pair is not None:
        strategies = _prefer_strategies(
            strategies=strategies,
            preferred=("overflow_length", "heavy_zero_pad", "tail_digit_burst"),
        )
    strategy = rng.choice(strategies)
    replacement = token

    if strategy == "heavy_zero_pad":
        replacement = ("0" * rng.randint(4, 12)) + token.lstrip("+-")
        if token.startswith(("+", "-")) and rng.choice(("keep_sign", "drop_sign")) == "keep_sign":
            replacement = token[0] + replacement
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
        if slash_mask:
            replacement = token + (growth_char * rng.randint(3, 12))
        else:
            replacement = token + (growth_char * rng.randint(1, 3))
    elif strategy == "tail_digit_burst":
        growth_char = token[-1] if token else "0"
        if slash_mask:
            replacement = token + (growth_char * rng.randint(12, 48))
        else:
            replacement = token + (growth_char * rng.randint(4, 12))

    if replacement == token:
        return None
    candidate = text[:start] + replacement + text[end:]
    if candidate != text:
        return sanitize_mutated_text(candidate)
    return None


def _extreme_numeric_surgery(
    *,
    text: str,
    rng: random.Random,
) -> str | None:
    numeric_ranges = _find_numeric_ranges(text=text)
    if not numeric_ranges:
        return None
    start, end = rng.choice(numeric_ranges)
    token = text[start:end]
    body = token[1:] if token.startswith("-") else token
    if not body:
        return None
    observed_values, widths = _observed_numeric_values(text=text)

    strategy = rng.choice(
        (
            "force_negative",
            "burst_length",
            "zero_pad_heavily",
            "duplicate_token",
            "spam_boundary_pair",
            "signed_zero_burst",
        )
    )
    replacement = token

    if strategy == "force_negative":
        replacement = "-" + token if not token.startswith("-") else "--" + body
    elif strategy == "burst_length":
        growth_char = body[-1] if body else "0"
        replacement = token + (growth_char * rng.randint(8, 24))
    elif strategy == "zero_pad_heavily":
        replacement = ("0" * rng.randint(8, 24)) + body
        if token.startswith("-") and rng.choice(("keep_sign", "drop_sign")) == "keep_sign":
            replacement = "-" + replacement
    elif strategy == "duplicate_token":
        repeated = body * rng.randint(4, 12)
        replacement = "-" + repeated if token.startswith("-") else repeated
    elif strategy == "spam_boundary_pair":
        width = max(1, max(widths, default=len(body)))
        candidate_suffixes = [str((10 ** width) - 1), str(10 ** width)]
        if observed_values:
            candidate_suffixes.extend(
                str(candidate)
                for candidate in (
                    min(observed_values) - 1,
                    max(observed_values) + 1,
                )
            )
        deduped_suffixes: list[str] = []
        for suffix in candidate_suffixes:
            if suffix == body or suffix in deduped_suffixes:
                continue
            deduped_suffixes.append(suffix)
        suffix = rng.choice(deduped_suffixes or [body * 2])
        repeated = (body + suffix) * rng.randint(2, 5)
        replacement = "-" + repeated if token.startswith("-") else repeated
    else:
        replacement = "-" + ("0" * rng.randint(6, 18)) + body

    if replacement == token:
        return None
    return _replace_text_range(
        text=text,
        start=start,
        end=end,
        replacement=replacement,
    )


def _ultra_long_numeric_surgery(
    *,
    text: str,
    rng: random.Random,
) -> str | None:
    numeric_ranges = _find_numeric_ranges(text=text)
    if not numeric_ranges:
        return None
    start, end = rng.choice(numeric_ranges)
    token = text[start:end]
    sign = "-" if token.startswith("-") else ""
    body = token.lstrip("+-")
    if not body or not body.isdigit():
        return None

    observed_values, widths = _observed_numeric_values(text=text)
    target_digits = rng.randint(
        _ULTRA_LONG_NUMERIC_MIN_DIGITS,
        _ULTRA_LONG_NUMERIC_MAX_DIGITS,
    )
    strategy = rng.choice(
        (
            "zero_flood",
            "repeat_body",
            "boundary_pair_flood",
        )
    )

    if strategy == "zero_flood":
        replacement_body = "0" * target_digits
    elif strategy == "repeat_body":
        replacement_body = _repeat_text_to_length(
            seed=body,
            length=target_digits,
            filler=body[-1],
        )
    else:
        width = max(1, max(widths, default=len(body)))
        candidate_suffixes = [str((10 ** width) - 1), str(10 ** width)]
        if observed_values:
            candidate_suffixes.extend(
                str(abs(candidate))
                for candidate in (
                    min(observed_values) - 1,
                    max(observed_values) + 1,
                )
            )
        seed_parts = [body]
        for suffix in candidate_suffixes:
            normalized = suffix.lstrip("+-")
            if not normalized or normalized == body or normalized in seed_parts:
                continue
            seed_parts.append(normalized)
        replacement_body = _repeat_text_to_length(
            seed="".join(seed_parts),
            length=target_digits,
            filler="9",
        )

    replacement = sign + replacement_body
    if replacement == token:
        return None
    return _replace_text_range(
        text=text,
        start=start,
        end=end,
        replacement=replacement,
    )


def _neighbor_boundary_numeric_surgery(
    *,
    text: str,
    rng: random.Random,
) -> str | None:
    numeric_ranges = _find_numeric_ranges(text=text)
    if not numeric_ranges:
        return None
    start, end = rng.choice(numeric_ranges)
    token = text[start:end]
    try:
        value = int(token)
    except ValueError:
        return None
    observed_values, widths = _observed_numeric_values(text=text)
    width = max(1, len(token.lstrip("+-")))
    observed_width = max(widths, default=width)
    magnitude_boundaries = {
        0,
        1,
        10 ** max(0, width - 1),
        (10 ** width) - 1,
        10 ** width,
        10 ** observed_width,
        (10 ** observed_width) - 1,
    }
    observed_boundaries = set(observed_values)
    if observed_values:
        observed_boundaries.update(
            {
                min(observed_values) - 1,
                min(observed_values) + 1,
                max(observed_values) - 1,
                max(observed_values) + 1,
            }
        )
    boundary_candidates = tuple(
        candidate
        for candidate in sorted(magnitude_boundaries | observed_boundaries)
        if candidate != value
    )
    strategy = rng.choice(("off_by_one", "nearest_common", "sign_flip"))
    ranked_boundaries = [
        boundary
        for boundary in sorted(
            boundary_candidates,
            key=lambda boundary: (abs(boundary - value), boundary),
        )
        if boundary != value
    ]

    if strategy == "off_by_one":
        choices = [value - 1, value + 1]
        choices.extend(ranked_boundaries[:2])
    elif strategy == "nearest_common":
        choices = ranked_boundaries[:8]
    else:
        choices = [-(abs(value) + 1), -abs(value), 0, 1]
        if value < 0:
            choices.extend([abs(value), max(0, abs(value) - 1)])

    deduped_choices: list[int] = []
    for choice in choices:
        if choice == value or choice in deduped_choices:
            continue
        deduped_choices.append(choice)
    if not deduped_choices:
        return None

    replacement = str(rng.choice(deduped_choices))
    return _replace_text_range(
        text=text,
        start=start,
        end=end,
        replacement=replacement,
    )


def _replace_numeric_tail_with_boundary(
    *,
    text: str,
    rng: random.Random,
    capabilities: GrammarCapabilities,
) -> str | None:
    numeric_ranges = _find_numeric_ranges(text=text)
    if not numeric_ranges:
        return None

    supported_boundaries = _boundary_chars(capabilities=capabilities)
    scored_ranges: list[tuple[int, int, int]] = []
    for start, end in numeric_ranges:
        score = 0
        if start == 0 or end == len(text):
            score += 2
        if start > 0 and text[start - 1] in supported_boundaries:
            score += 2
        if end < len(text) and text[end] in supported_boundaries:
            score += 2
        if start > 0 and text[start - 1] in {"/", "-"}:
            score += 1
        scored_ranges.append((score, start, end))

    best_score = max(score for score, _start, _end in scored_ranges)
    candidate_ranges = [
        (start, end)
        for score, start, end in scored_ranges
        if score == best_score
    ] or numeric_ranges
    start, end = rng.choice(candidate_ranges)
    token = text[start:end]
    body = token.lstrip("+-")
    if not body or not body.isdigit():
        return None
    try:
        value = int(token)
    except ValueError:
        return None

    observed_values, _widths = _observed_numeric_values(text=text)
    width = max(1, len(body))
    candidate_values = [
        value + 1,
        value - 1,
        (10 ** width) - 1,
        10 ** width,
        0,
        1,
    ]
    if observed_values:
        candidate_values.extend(
            (
                min(observed_values) - 1,
                max(observed_values) + 1,
            )
        )
    if width > 1:
        candidate_values.append(int(body[0] + ("0" * (width - 1))))

    deduped_values: list[int] = []
    for candidate_value in candidate_values:
        if candidate_value == value or candidate_value in deduped_values:
            continue
        deduped_values.append(candidate_value)
    if not deduped_values:
        return None

    replacement = str(rng.choice(deduped_values))
    return _replace_text_range(
        text=text,
        start=start,
        end=end,
        replacement=replacement,
    )


def _alphabetic_label_substitution(
    *,
    text: str,
    grammar_spec: GrammarSpec,
    max_depth: int,
    rng: random.Random,
    capabilities: GrammarCapabilities,
) -> str | None:
    label_choices: list[str] = []
    for _ in range(8):
        fragment = generate_from_grammar(
            grammar_spec=grammar_spec,
            max_depth=max_depth,
            rng=rng,
        )
        for _start, _end in _find_alnum_token_ranges(text=fragment, min_length=1):
            token = fragment[_start:_end]
            if token.isalpha() and token not in label_choices:
                label_choices.append(token)
    for length in (2, 3, 4, 5, 6, 8, 10, 12):
        if len(label_choices) >= 8:
            break
        fallback = _fallback_alpha_label(length=length)
        if fallback not in label_choices:
            label_choices.append(fallback)
    label_choices_tuple = tuple(label_choices)
    segmented_label_choices = [
        label
        for label in label_choices
        if len(label) >= 2
    ]
    segmented_label_choices_tuple = tuple(segmented_label_choices) or label_choices_tuple
    token_ranges = _find_alnum_token_ranges(text=text, min_length=1)
    if not token_ranges:
        return None

    separator_candidates = _separator_char_candidates(
        text=text,
        capabilities=capabilities,
        min_occurrences=1,
    )
    strategies = ["replace_token"]
    if separator_candidates:
        # Bias segmented inputs toward coherent alphabetic families.
        strategies = ["replace_family", "replace_run", "replace_run", "replace_token"]
    strategy = rng.choice(tuple(strategies))

    if strategy == "replace_run" and separator_candidates:
        separator = rng.choice(separator_candidates)
        parts = text.split(separator)
        non_empty_indexes = [index for index, part in enumerate(parts) if part]
        if len(non_empty_indexes) >= 2:
            replace_count = min(len(non_empty_indexes), max(2, rng.randint(2, 4)))
            candidate_parts = list(parts)
            for index in non_empty_indexes[:replace_count]:
                candidate_parts[index] = rng.choice(segmented_label_choices_tuple)
            candidate = separator.join(candidate_parts)
            if candidate != text:
                return sanitize_mutated_text(candidate)

    if strategy == "replace_family" and separator_candidates:
        separator = rng.choice(separator_candidates)
        parts = text.split(separator)
        non_empty_indexes = [index for index, part in enumerate(parts) if part]
        if len(non_empty_indexes) >= 2:
            candidate_parts = list(parts)
            for index in non_empty_indexes:
                candidate_parts[index] = rng.choice(segmented_label_choices_tuple)
            candidate = separator.join(candidate_parts)
            if candidate != text:
                return sanitize_mutated_text(candidate)

    start, end = rng.choice(token_ranges)
    return _replace_text_range(
        text=text,
        start=start,
        end=end,
        replacement=rng.choice(label_choices_tuple),
    )


def _mixed_separator_family_graft(
    *,
    original_text: str,
    grammar_spec: GrammarSpec,
    max_depth: int,
    rng: random.Random,
    capabilities: GrammarCapabilities,
) -> str | None:
    if len(capabilities.separator_chars) < 2:
        return None
    base_text = original_text or generate_from_grammar(
        grammar_spec=grammar_spec,
        max_depth=max_depth,
        rng=rng,
    )
    base_tokens = _tokenize_for_alternation_splice(text=base_text)
    base_separator_chars = _separator_chars_in_text(
        text=base_text,
        capabilities=capabilities,
    )
    if len(base_tokens) < 2 or not base_separator_chars:
        return None

    for _ in range(12):
        fragment = generate_from_grammar(
            grammar_spec=grammar_spec,
            max_depth=max_depth,
            rng=rng,
        )
        fragment_tokens = _tokenize_for_alternation_splice(text=fragment)
        fragment_separator_chars = _separator_chars_in_text(
            text=fragment,
            capabilities=capabilities,
        )
        if len(fragment_tokens) < 2 or not fragment_separator_chars:
            continue
        if len(base_separator_chars | fragment_separator_chars) < 2:
            continue
        prefix_count = rng.randrange(1, len(base_tokens))
        suffix_start = rng.randrange(1, len(fragment_tokens))
        candidate = "".join(base_tokens[:prefix_count] + fragment_tokens[suffix_start:])
        if not candidate or candidate in {original_text, base_text, fragment}:
            continue
        candidate_separator_chars = _separator_chars_in_text(
            text=candidate,
            capabilities=capabilities,
        )
        if len(candidate_separator_chars) < 2:
            continue
        if not (candidate_separator_chars & base_separator_chars):
            continue
        if not (candidate_separator_chars & fragment_separator_chars):
            continue
        return sanitize_mutated_text(candidate)
    return None


def _token_whitespace_surgery(
    *,
    text: str,
    rng: random.Random,
    capabilities: GrammarCapabilities,
) -> str | None:
    whitespace_payloads = (" ", "\t", "\n", "\r", " \t", "\t \n", "\n\n", " \t ")
    token_ranges = _find_alnum_token_ranges(text=text, min_length=2)
    separator_positions = _separator_indexes(text=text, capabilities=capabilities)
    strategy = rng.choice(
        ("split_token", "surround_token", "burst_separator", "burst_token")
    )

    if strategy == "split_token" and token_ranges:
        start, end = rng.choice(token_ranges)
        if end - start < 2:
            return None
        insert_at = rng.randrange(start + 1, end)
        payload = rng.choice(whitespace_payloads)
        return sanitize_mutated_text(text[:insert_at] + payload + text[insert_at:])

    if strategy == "burst_separator" and separator_positions:
        index = rng.choice(separator_positions)
        left_payload = rng.choice(whitespace_payloads)
        right_payload = rng.choice(whitespace_payloads)
        candidate = text[:index] + left_payload + text[index] + right_payload + text[index + 1 :]
        if candidate != text:
            return sanitize_mutated_text(candidate)
        return None

    if not token_ranges:
        return None
    start, end = rng.choice(token_ranges)
    payload = rng.choice(whitespace_payloads)
    direction = rng.choice(("prefix", "suffix")) if strategy == "surround_token" else "suffix"
    if strategy == "burst_token":
        burst = payload * rng.randint(2, 5)
        candidate = text[:start] + burst + text[start:end] + burst + text[end:]
    elif direction == "prefix":
        candidate = text[:start] + payload + text[start:]
    else:
        candidate = text[:end] + payload + text[end:]
    if candidate != text:
        return sanitize_mutated_text(candidate)
    return None


def _insert_boundary_whitespace_once(
    *,
    text: str,
    rng: random.Random,
    capabilities: GrammarCapabilities,
) -> str | None:
    payload = rng.choice((" ", "\t"))
    separator_positions = _separator_indexes(text=text, capabilities=capabilities)
    if separator_positions:
        index = rng.choice(separator_positions)
        insert_at = index if rng.choice(("before", "after")) == "before" else index + 1
        return sanitize_mutated_text(text[:insert_at] + payload + text[insert_at:])

    transition_positions = [
        index
        for index in range(1, len(text))
        if not text[index - 1].isspace()
        and not text[index].isspace()
        and (text[index - 1].isalnum() != text[index].isalnum())
    ]
    if not transition_positions:
        return None
    insert_at = rng.choice(transition_positions)
    return sanitize_mutated_text(text[:insert_at] + payload + text[insert_at:])


def _extend_delimited_list_once(
    *,
    text: str,
    rng: random.Random,
    capabilities: GrammarCapabilities,
) -> str | None:
    if not capabilities.paired_delimiters:
        return None

    delimited_ranges = _find_delimited_content_ranges(
        text=text,
        capabilities=capabilities,
    )
    if not delimited_ranges:
        return None

    prioritized_ranges = [
        range_
        for range_ in delimited_ranges
        if _separator_char_candidates(
            text=text[range_[0]:range_[1]],
            capabilities=capabilities,
            min_occurrences=1,
        )
    ]
    candidate_ranges = prioritized_ranges or delimited_ranges
    inner_start, inner_end, _opener, _closer = rng.choice(candidate_ranges)
    inner = text[inner_start:inner_end]
    separator_candidates = _separator_char_candidates(
        text=inner,
        capabilities=capabilities,
        min_occurrences=1,
    )
    if not separator_candidates:
        return None
    separator = rng.choice(separator_candidates)
    parts = [part for part in inner.split(separator) if part]
    if not parts:
        return None
    repeated_part = rng.choice(parts)
    replacement = inner + separator + repeated_part
    return _replace_text_range(
        text=text,
        start=inner_start,
        end=inner_end,
        replacement=replacement,
    )

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
                            non_alnum_chars=frozenset(),
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
                            non_alnum_chars=frozenset(),
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
        strategy_entries.append(
            (
                "neighbor_boundary_numeric_surgery",
                lambda: _neighbor_boundary_numeric_surgery(
                    text=original_text,
                    rng=rng,
                ),
                2.0,
            )
        )
        strategy_entries.append(
            (
                "extreme_numeric_surgery",
                lambda: _extreme_numeric_surgery(text=original_text, rng=rng),
                2.0,
            )
        )
        strategy_entries.append(
            (
                "ultra_long_numeric_surgery",
                lambda: _ultra_long_numeric_surgery(text=original_text, rng=rng),
                0.2,
            )
        )
    if capabilities is not None and capabilities.separator_chars:
        strategy_entries.extend(
            (
                (
                    "repetition_amplification_surgery",
                    lambda: _repetition_amplification_surgery(
                        text=original_text,
                        rng=rng,
                        capabilities=capabilities,
                    ),
                    2.1,
                ),
                (
                    "alphabetic_label_substitution",
                    lambda: _alphabetic_label_substitution(
                        text=original_text,
                        rng=rng,
                        capabilities=capabilities,
                    ),
                    1.9,
                ),
                (
                    "separator_run_surgery",
                    lambda: _separator_run_surgery(
                        text=original_text,
                        rng=rng,
                        capabilities=capabilities,
                    ),
                    2.2,
                ),
                (
                    "token_whitespace_surgery",
                    lambda: _token_whitespace_surgery(
                        text=original_text,
                        rng=rng,
                        capabilities=capabilities,
                    ),
                    2.1,
                ),
            )
        )
    if capabilities is not None and capabilities.has_numeric_literals and "-" in capabilities.literal_chars:
        strategy_entries.append(
            (
                "descending_range_surgery",
                lambda: _descending_range_surgery(
                    text=original_text,
                    rng=rng,
                    capabilities=capabilities,
                ),
                1.8,
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
    "_extreme_numeric_surgery",
    "_ultra_long_numeric_surgery",
    "_neighbor_boundary_numeric_surgery",
    "_segment_count_change",
    "_separator_confusion",
    "_separator_run_surgery",
    "_repetition_amplification_surgery",
    "_alphabetic_label_substitution",
    "_mixed_separator_family_graft",
    "_token_whitespace_surgery",
    "_descending_range_surgery",
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
