from __future__ import annotations

import random
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

GrammarRules: TypeAlias = dict[str, list[str]]
GrammarSpec: TypeAlias = dict[str, object]
AstGrammarRules: TypeAlias = dict[str, str]
AstGrammarSpec: TypeAlias = dict[str, object]
_TEXT_BOM_PREFIX = "\ufeff"

_NON_TERMINAL_PATTERN = re.compile(r"<[^<>]+>")
_TOKEN_SPLIT_PATTERN = re.compile(r"[A-Za-z0-9]+|[^A-Za-z0-9]+")
_NUMBER_RANGE_REF_PATTERN = re.compile(r"<number_range\b[^<>]*>", re.IGNORECASE)
_INTERESTING_BYTE_VALUES = (0x01, 0x0A, 0x0D, 0x20, 0x7F, 0x80, 0xFE, 0xFF)
_GRAMMARS_DIR = Path(__file__).resolve().parent.parent.parent / "grammars"
_SUPPORTED_DELIMITER_PAIRS = (("(", ")"), ("[", "]"), ("{", "}"), ("<", ">"))
_SUPPORTED_QUOTE_CHARS = frozenset({'"', "'", "`"})
_SUPPORTED_SEPARATOR_CHARS = frozenset({",", ".", ":", ";", "/", "\\", "|", "-", "_"})
_FOREIGN_PUNCTUATION_CHARS = tuple("?*()[]%-,")
_CONTROL_BURST_CHARS = ("\n", "\r", "\t", "\x01", "\x02", "\x1f", "\x7f")
_UPPERCASE_LITERAL_FALLBACK = tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_NUMERIC_RULE_NAME_TOKENS = (
    "digit",
    "number",
    "int",
    "float",
    "hex",
    "octet",
    "cidr",
    "prefix",
)
_SPECIAL_NUMERIC_LITERALS = ("NaN", "Infinity", "-Infinity")
_SURROGATE_PAIR_ESCAPE_PAYLOADS = ("\\uD83D\\uDE00", "\\uD834\\uDD1E")
_INVALID_SURROGATE_ESCAPE_PAYLOADS = ("\\uD83D\\u0041", "\\uD800\\u0030")
_EXPONENT_SUFFIXES = ("e0", "e+10", "E-1")
_TEXT_REGENERATION_ATTEMPTS = 32
GrammarOperatorApply: TypeAlias = Callable[
    [str, str, GrammarSpec, int, random.Random, "GrammarCapabilities"],
    str | None,
]
GrammarCapabilityPredicate: TypeAlias = Callable[["GrammarCapabilities"], bool]

@dataclass(frozen=True)
class GrammarCapabilities:
    literal_chars: frozenset[str]
    non_alnum_chars: frozenset[str]
    separator_chars: frozenset[str]
    quote_chars: frozenset[str]
    paired_delimiters: tuple[tuple[str, str], ...]
    has_numeric_literals: bool
    has_number_ranges: bool
    has_repetition: bool
    has_alternation: bool
    has_delimiter_literals: bool
    has_exact_parse_path: bool
    recursive_nonterminals: frozenset[str]
    has_recursive_nonterminals: bool
    has_recursive_rules: bool

@dataclass(frozen=True)
class GrammarOperatorSpec:
    name: str
    valid_weight: float
    invalid_weight: float
    supports: GrammarCapabilityPredicate
    apply: GrammarOperatorApply

def sanitize_mutated_text(text: str) -> str:
    """Remove disallowed NULs from mutator output without changing other bytes."""
    if "\x00" not in text:
        return text
    return text.replace("\x00", "")


def regenerate_text_without_nul(
    producer: Callable[[], str],
    *,
    attempts: int = _TEXT_REGENERATION_ATTEMPTS,
    fallback: str = "1",
) -> str:
    """Retry a text producer until it yields a NUL-free, non-empty candidate."""
    max_attempts = max(1, int(attempts))
    last_candidate = ""
    for _ in range(max_attempts):
        candidate = producer()
        last_candidate = candidate
        if candidate and "\x00" not in candidate:
            return candidate
    sanitized = sanitize_mutated_text(last_candidate)
    if sanitized:
        return sanitized
    sanitized_fallback = sanitize_mutated_text(fallback)
    return sanitized_fallback or "1"

def sanitize_mutated_bytes(data: bytes | bytearray) -> bytes:
    """Replace disallowed NUL bytes while preserving payload length."""
    if 0 not in data:
        return bytes(data)
    return bytes(0x01 if byte == 0 else byte for byte in data)

__all__ = [
    "AstGrammarRules",
    "AstGrammarSpec",
    "GrammarRules",
    "GrammarSpec",
    "GrammarCapabilities",
    "GrammarOperatorSpec",
    "sanitize_mutated_text",
    "sanitize_mutated_bytes",
    "regenerate_text_without_nul",
    "_TEXT_BOM_PREFIX",
    "_NON_TERMINAL_PATTERN",
    "_TOKEN_SPLIT_PATTERN",
    "_NUMBER_RANGE_REF_PATTERN",
    "_INTERESTING_BYTE_VALUES",
    "_GRAMMARS_DIR",
    "_SUPPORTED_DELIMITER_PAIRS",
    "_SUPPORTED_QUOTE_CHARS",
    "_SUPPORTED_SEPARATOR_CHARS",
    "_FOREIGN_PUNCTUATION_CHARS",
    "_CONTROL_BURST_CHARS",
    "_UPPERCASE_LITERAL_FALLBACK",
    "_NUMERIC_RULE_NAME_TOKENS",
    "_SPECIAL_NUMERIC_LITERALS",
    "_SURROGATE_PAIR_ESCAPE_PAYLOADS",
    "_INVALID_SURROGATE_ESCAPE_PAYLOADS",
    "_EXPONENT_SUFFIXES",
    "_TEXT_REGENERATION_ATTEMPTS",
    "GrammarOperatorApply",
    "GrammarCapabilityPredicate",
]
