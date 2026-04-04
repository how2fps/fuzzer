from __future__ import annotations

import json
import random
from pathlib import Path

from .shared import (
    AstGrammarRules,
    AstGrammarSpec,
    GrammarCapabilities,
    GrammarRules,
    GrammarSpec,
    regenerate_text_without_nul,
    _GRAMMARS_DIR,
    _NON_TERMINAL_PATTERN,
    _NUMBER_RANGE_REF_PATTERN,
    _NUMERIC_RULE_NAME_TOKENS,
    _SUPPORTED_DELIMITER_PAIRS,
    _SUPPORTED_QUOTE_CHARS,
    _SUPPORTED_SEPARATOR_CHARS,
    sanitize_mutated_text,
)

def _production_references(*, production: str) -> tuple[str, ...]:
    return tuple(match.group(0) for match in _NON_TERMINAL_PATTERN.finditer(production))

def _build_rule_graph(*, rules: GrammarRules) -> dict[str, tuple[str, ...]]:
    graph: dict[str, tuple[str, ...]] = {}
    known_rules = set(rules)
    for symbol, productions in rules.items():
        ordered_refs: list[str] = []
        seen_refs: set[str] = set()
        for production in productions:
            for ref in _production_references(production=production):
                if ref in known_rules and ref not in seen_refs:
                    ordered_refs.append(ref)
                    seen_refs.add(ref)
        graph[symbol] = tuple(ordered_refs)
    return graph

def _reachable_rule_symbols(
    *,
    start: str,
    graph: dict[str, tuple[str, ...]],
) -> set[str]:
    if start not in graph:
        return set()
    seen: set[str] = set()
    stack = [start]
    while stack:
        symbol = stack.pop()
        if symbol in seen:
            continue
        seen.add(symbol)
        stack.extend(graph.get(symbol, ()))
    return seen

def _productive_rule_symbols(*, rules: GrammarRules) -> set[str]:
    productive: set[str] = set()
    known_rules = set(rules)
    changed = True
    while changed:
        changed = False
        for symbol, productions in rules.items():
            if symbol in productive:
                continue
            for production in productions:
                refs = _production_references(production=production)
                if any(ref not in known_rules for ref in refs):
                    continue
                if all(ref in productive for ref in refs):
                    productive.add(symbol)
                    changed = True
                    break
    return productive

def _can_reach_symbol(
    *,
    source: str,
    target: str,
    graph: dict[str, tuple[str, ...]],
) -> bool:
    seen: set[str] = set()
    stack = list(graph.get(source, ()))
    while stack:
        symbol = stack.pop()
        if symbol == target:
            return True
        if symbol in seen:
            continue
        seen.add(symbol)
        stack.extend(graph.get(symbol, ()))
    return False

def _recursive_rule_symbols(*, graph: dict[str, tuple[str, ...]]) -> set[str]:
    return {
        symbol
        for symbol in graph
        if _can_reach_symbol(source=symbol, target=symbol, graph=graph)
    }

def _unresolved_references(
    *,
    rules: GrammarRules,
    reachable_symbols: set[str],
) -> set[str]:
    known_rules = set(rules)
    unresolved: set[str] = set()
    for symbol in reachable_symbols:
        for production in rules.get(symbol, []):
            unresolved.update(
                ref
                for ref in _production_references(production=production)
                if ref not in known_rules
            )
    return unresolved

def _production_has_repetition_shape(
    *,
    production: str,
    recursive_nonterminals: set[str],
    known_rules: set[str],
) -> bool:
    refs = [
        ref for ref in _production_references(production=production) if ref in known_rules
    ]
    if not refs:
        return False
    seen_refs: set[str] = set()
    for ref in refs:
        if ref in seen_refs:
            return True
        seen_refs.add(ref)
    literal_fragment = _NON_TERMINAL_PATTERN.sub("", production)
    return any(ref in recursive_nonterminals for ref in refs) and (
        bool(literal_fragment) or len(refs) > 1
    )

def _terminal_fragments_from_grammar_spec(
    *, grammar_spec: GrammarSpec | dict[str, object]
) -> tuple[str, ...]:
    normalized = normalize_grammar_spec(grammar_spec=grammar_spec)
    rules = normalized["rules"]
    graph = _build_rule_graph(rules=rules)
    reachable_symbols = _reachable_rule_symbols(start=normalized["start"], graph=graph)
    symbols_to_scan = reachable_symbols or set(rules)
    return tuple(
        _NON_TERMINAL_PATTERN.sub("", option)
        for symbol, options in rules.items()
        if symbol in symbols_to_scan
        for option in options
    )

def grammar_capabilities(
    *, grammar_spec: GrammarSpec | dict[str, object]
) -> GrammarCapabilities:
    normalized = normalize_grammar_spec(grammar_spec=grammar_spec)
    rules = normalized["rules"]
    graph = _build_rule_graph(rules=rules)
    reachable_symbols = _reachable_rule_symbols(start=normalized["start"], graph=graph)
    symbols_to_scan = reachable_symbols or set(rules)
    fragments = _terminal_fragments_from_grammar_spec(grammar_spec=normalized)
    literal_chars = frozenset(
        char
        for fragment in fragments
        for char in fragment
        if char != "\x00"
    )
    inferred_recursive_nonterminals = _recursive_rule_symbols(graph=graph)
    recursive_nonterminals = frozenset(
        set(normalized["recursive_symbols"]) | inferred_recursive_nonterminals
    )
    rule_names = {
        symbol.strip("<>").lower()
        for symbol in symbols_to_scan
    }
    paired_delimiters = tuple(
        pair
        for pair in _SUPPORTED_DELIMITER_PAIRS
        if pair[0] in literal_chars and pair[1] in literal_chars
    )
    unresolved_refs = _unresolved_references(rules=rules, reachable_symbols=symbols_to_scan)
    productive_symbols = _productive_rule_symbols(rules=rules)
    has_number_ranges = any(
        _NUMBER_RANGE_REF_PATTERN.fullmatch(ref) is not None
        for ref in unresolved_refs
    )
    has_numeric_literals = has_number_ranges or any(char.isdigit() for char in literal_chars) or any(
        token in name
        for name in rule_names
        for token in _NUMERIC_RULE_NAME_TOKENS
    )
    has_alternation = any(
        len(rules[symbol]) > 1
        for symbol in symbols_to_scan
    )
    has_repetition = any(
        _production_has_repetition_shape(
            production=production,
            recursive_nonterminals=set(recursive_nonterminals),
            known_rules=set(rules),
        )
        for symbol in symbols_to_scan
        for production in rules[symbol]
    )
    has_delimiter_literals = bool(
        paired_delimiters
        or any(char in _SUPPORTED_QUOTE_CHARS for char in literal_chars)
        or any(char in _SUPPORTED_SEPARATOR_CHARS for char in literal_chars)
    )
    has_recursive_nonterminals = bool(recursive_nonterminals)
    has_exact_parse_path = (
        normalized["start"] in rules
        and normalized["start"] in productive_symbols
        and symbols_to_scan.issubset(productive_symbols)
        and not unresolved_refs
    )
    return GrammarCapabilities(
        literal_chars=literal_chars,
        separator_chars=frozenset(
            char for char in literal_chars if char in _SUPPORTED_SEPARATOR_CHARS
        ),
        quote_chars=frozenset(
            char for char in literal_chars if char in _SUPPORTED_QUOTE_CHARS
        ),
        paired_delimiters=paired_delimiters,
        has_numeric_literals=has_numeric_literals,
        has_number_ranges=has_number_ranges,
        has_repetition=has_repetition,
        has_alternation=has_alternation,
        has_delimiter_literals=has_delimiter_literals,
        has_exact_parse_path=has_exact_parse_path,
        recursive_nonterminals=recursive_nonterminals,
        has_recursive_nonterminals=has_recursive_nonterminals,
        has_recursive_rules=has_recursive_nonterminals,
    )

def _normalize_recursive_symbols(
    *, value: object, source: str
) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, set):
        symbols = value
    elif isinstance(value, (list, tuple)):
        symbols = value
    else:
        raise TypeError(
            f"{source}: grammar_spec['recursive_symbols'] must be a set or list"
        )

    normalized_symbols: set[str] = set()
    for symbol in symbols:
        if not isinstance(symbol, str):
            raise TypeError(
                f"{source}: grammar_spec['recursive_symbols'] entries must be strings"
            )
        normalized_symbols.add(symbol)
    return normalized_symbols

def _normalize_rules(*, value: object, source: str) -> GrammarRules:
    if not isinstance(value, dict):
        raise TypeError(f"{source}: grammar_spec['rules'] must be a dictionary")

    normalized_rules: GrammarRules = {}
    for symbol, productions in value.items():
        if not isinstance(symbol, str):
            raise TypeError(f"{source}: grammar rule names must be strings")
        if not isinstance(productions, list) or not productions:
            raise TypeError(
                f"{source}: grammar_spec['rules'][{symbol!r}] must be a non-empty list"
            )
        if not all(isinstance(option, str) for option in productions):
            raise TypeError(
                f"{source}: grammar_spec['rules'][{symbol!r}] entries must be strings"
            )
        normalized_rules[symbol] = list(productions)
    return normalized_rules

def normalize_grammar_spec(
    *, grammar_spec: GrammarSpec | dict[str, object], source: str = "<memory>"
) -> GrammarSpec:
    if not isinstance(grammar_spec, dict):
        raise TypeError(f"{source}: grammar_spec must be a dictionary")

    start = grammar_spec.get("start")
    if not isinstance(start, str):
        raise TypeError(f"{source}: grammar_spec['start'] must be a string")

    rules = _normalize_rules(value=grammar_spec.get("rules"), source=source)
    recursive_symbols = _normalize_recursive_symbols(
        value=grammar_spec.get("recursive_symbols", []),
        source=source,
    )
    return {
        "start": start,
        "rules": rules,
        "recursive_symbols": recursive_symbols,
    }

def _normalize_ast_rule_name(symbol: str) -> str:
    if symbol.startswith("<") and symbol.endswith(">") and len(symbol) >= 3:
        return symbol[1:-1]
    return symbol

def _production_to_ast_expression(*, production: str) -> str:
    parts: list[str] = []
    last_index = 0
    for match in _NON_TERMINAL_PATTERN.finditer(production):
        literal_fragment = production[last_index:match.start()]
        if literal_fragment:
            parts.append(json.dumps(literal_fragment))
        parts.append(match.group(0))
        last_index = match.end()
    trailing_fragment = production[last_index:]
    if trailing_fragment:
        parts.append(json.dumps(trailing_fragment))
    if not parts:
        return json.dumps("")
    return " ".join(parts)

def grammar_spec_to_ast_grammar_spec(
    *, grammar_spec: GrammarSpec | dict[str, object], source: str = "<memory>"
) -> AstGrammarSpec:
    normalized = normalize_grammar_spec(grammar_spec=grammar_spec, source=source)
    graph = _build_rule_graph(rules=normalized["rules"])
    recursive_symbols = {
        _normalize_ast_rule_name(symbol)
        for symbol in (
            set(normalized["recursive_symbols"]) | _recursive_rule_symbols(graph=graph)
        )
    }
    ast_rules: AstGrammarRules = {
        _normalize_ast_rule_name(symbol): " | ".join(
            _production_to_ast_expression(production=production)
            for production in productions
        )
        for symbol, productions in normalized["rules"].items()
    }
    return {
        "start": _normalize_ast_rule_name(normalized["start"]),
        "rules": ast_rules,
        "recursive_symbols": recursive_symbols,
    }

def _normalize_ast_rules(*, value: object, source: str) -> AstGrammarRules:
    if not isinstance(value, dict):
        raise TypeError(f"{source}: ast_grammar_spec['rules'] must be a dictionary")

    normalized_rules: AstGrammarRules = {}
    for symbol, expr in value.items():
        if not isinstance(symbol, str):
            raise TypeError(f"{source}: ast grammar rule names must be strings")
        if not isinstance(expr, str) or not expr:
            raise TypeError(
                f"{source}: ast_grammar_spec['rules'][{symbol!r}] must be a non-empty string"
            )
        normalized_rules[_normalize_ast_rule_name(symbol)] = expr
    return normalized_rules

def normalize_ast_grammar_spec(
    *,
    ast_grammar_spec: AstGrammarSpec | dict[str, object],
    source: str = "<memory>",
) -> AstGrammarSpec:
    if not isinstance(ast_grammar_spec, dict):
        raise TypeError(f"{source}: ast_grammar_spec must be a dictionary")

    start = ast_grammar_spec.get("start")
    if not isinstance(start, str):
        raise TypeError(f"{source}: ast_grammar_spec['start'] must be a string")

    recursive_symbols = _normalize_recursive_symbols(
        value=ast_grammar_spec.get("recursive_symbols", []),
        source=source,
    )
    return {
        "start": _normalize_ast_rule_name(start),
        "rules": _normalize_ast_rules(
            value=ast_grammar_spec.get("rules"),
            source=source,
        ),
        "recursive_symbols": {
            _normalize_ast_rule_name(symbol) for symbol in recursive_symbols
        },
    }

def load_grammar_from_json(*, path: str | Path) -> GrammarSpec:
    grammar_path = Path(path)
    with grammar_path.open(encoding="utf-8") as file:
        raw_spec = json.load(file)
    return normalize_grammar_spec(grammar_spec=raw_spec, source=str(grammar_path))

def load_ast_grammar_from_json(*, path: str | Path) -> AstGrammarSpec:
    grammar_path = Path(path)
    with grammar_path.open(encoding="utf-8") as file:
        raw_spec = json.load(file)
    return normalize_ast_grammar_spec(
        ast_grammar_spec=raw_spec,
        source=str(grammar_path),
    )

def configure_runtime_grammar(
    *,
    kind: str,
    grammar_path: str | Path | None = None,
    ast_grammar_path: str | Path | None = None,
) -> None:
    global _RUNTIME_GRAMMAR_VERSION
    if kind not in _ACTIVE_GRAMMARS:
        raise ValueError(
            f"Unsupported grammar kind {kind!r}. Must be one of: {sorted(_ACTIVE_GRAMMARS)}"
        )
    if grammar_path is None and ast_grammar_path is None:
        _ACTIVE_GRAMMARS[kind] = _DEFAULT_GRAMMARS[kind]
        _ACTIVE_AST_GRAMMARS[kind] = _DEFAULT_AST_GRAMMARS[kind]
        _RUNTIME_GRAMMAR_VERSION += 1
        return
    if grammar_path is not None:
        grammar_spec = load_grammar_from_json(path=grammar_path)
        _ACTIVE_GRAMMARS[kind] = grammar_spec
    else:
        grammar_spec = _ACTIVE_GRAMMARS[kind]

    if ast_grammar_path is not None:
        _ACTIVE_AST_GRAMMARS[kind] = load_ast_grammar_from_json(path=ast_grammar_path)
    elif grammar_path is not None:
        _ACTIVE_AST_GRAMMARS[kind] = grammar_spec_to_ast_grammar_spec(
            grammar_spec=grammar_spec,
            source=str(grammar_path),
        )
    _RUNTIME_GRAMMAR_VERSION += 1

def _resolve_grammar_spec(*, kind: str, grammar_path: str | Path | None = None) -> GrammarSpec:
    if grammar_path is not None:
        return load_grammar_from_json(path=grammar_path)
    return _ACTIVE_GRAMMARS[kind]

def resolve_grammar_spec(*, kind: str, grammar_path: str | Path | None = None) -> GrammarSpec:
    return _resolve_grammar_spec(kind=kind, grammar_path=grammar_path)

def resolve_ast_grammar_spec(
    *, kind: str, ast_grammar_path: str | Path | None = None
) -> AstGrammarSpec:
    if ast_grammar_path is not None:
        return load_ast_grammar_from_json(path=ast_grammar_path)
    return _ACTIVE_AST_GRAMMARS[kind]

def runtime_grammar_version() -> int:
    return _RUNTIME_GRAMMAR_VERSION

def _pick_production(
    *,
    symbol: str,
    rules: GrammarRules,
    recursive_symbols: set[str],
    depth: int,
    max_depth: int,
    rng: random.Random,
) -> str:
    productions = rules[symbol]
    if depth < max_depth or symbol not in recursive_symbols:
        return rng.choice(productions)

    safe_productions = [
        option
        for option in productions
        if not any(token in recursive_symbols for token in _NON_TERMINAL_PATTERN.findall(option))
    ]
    return rng.choice(safe_productions or productions)

def _expand_symbol(
    *,
    symbol: str,
    rules: GrammarRules,
    recursive_symbols: set[str],
    depth: int,
    max_depth: int,
    rng: random.Random,
) -> str:
    if symbol not in rules:
        return symbol

    production = _pick_production(
        symbol=symbol,
        rules=rules,
        recursive_symbols=recursive_symbols,
        depth=depth,
        max_depth=max_depth,
        rng=rng,
    )
    return _expand_production(
        production=production,
        rules=rules,
        recursive_symbols=recursive_symbols,
        depth=depth,
        max_depth=max_depth,
        rng=rng,
    )

def _expand_production(
    *,
    production: str,
    rules: GrammarRules,
    recursive_symbols: set[str],
    depth: int,
    max_depth: int,
    rng: random.Random,
) -> str:
    parts: list[str] = []
    last_idx = 0

    for match in _NON_TERMINAL_PATTERN.finditer(production):
        parts.append(production[last_idx:match.start()])
        next_symbol = match.group(0)
        parts.append(
            _expand_symbol(
                symbol=next_symbol,
                rules=rules,
                recursive_symbols=recursive_symbols,
                depth=depth + 1,
                max_depth=max_depth,
                rng=rng,
            )
        )
        last_idx = match.end()

    parts.append(production[last_idx:])
    return "".join(parts)

def _expand_symbol_with_production_index(
    *,
    symbol: str,
    production_index: int,
    rules: GrammarRules,
    recursive_symbols: set[str],
    depth: int,
    max_depth: int,
    rng: random.Random,
) -> str:
    if symbol not in rules:
        return symbol
    productions = rules[symbol]
    bounded_index = max(0, min(production_index, len(productions) - 1))
    return _expand_production(
        production=productions[bounded_index],
        rules=rules,
        recursive_symbols=recursive_symbols,
        depth=depth,
        max_depth=max_depth,
        rng=rng,
    )

def generate_from_grammar(
    *,
    grammar_spec: GrammarSpec,
    max_depth: int = 5,
    rng: random.Random | None = None,
) -> str:
    if max_depth < 1:
        raise ValueError("max_depth must be >= 1")

    normalized_spec = normalize_grammar_spec(grammar_spec=grammar_spec)
    random_engine = rng or random.Random()
    start = normalized_spec["start"]
    rules = normalized_spec["rules"]
    recursive_symbols = normalized_spec["recursive_symbols"]

    return regenerate_text_without_nul(
        lambda: sanitize_mutated_text(
            _expand_symbol(
                symbol=start,
                rules=rules,
                recursive_symbols=recursive_symbols,
                depth=0,
                max_depth=max_depth,
                rng=random_engine,
            )
        )
    )

JSON_GRAMMAR_PATH = _GRAMMARS_DIR / "json.json"
IP_GRAMMAR_PATH = _GRAMMARS_DIR / "ip.json"
IPV4_GRAMMAR_PATH = _GRAMMARS_DIR / "ipv4.json"
IPV6_GRAMMAR_PATH = _GRAMMARS_DIR / "ipv6.json"
JSON_AST_GRAMMAR_PATH = _GRAMMARS_DIR / "json.ast.json"
IP_AST_GRAMMAR_PATH = _GRAMMARS_DIR / "ip.ast.json"
IPV4_AST_GRAMMAR_PATH = _GRAMMARS_DIR / "ipv4.ast.json"
IPV6_AST_GRAMMAR_PATH = _GRAMMARS_DIR / "ipv6.ast.json"

JSON_GRAMMAR = load_grammar_from_json(path=JSON_GRAMMAR_PATH)
IP_GRAMMAR = load_grammar_from_json(path=IP_GRAMMAR_PATH)
IPV4_GRAMMAR = load_grammar_from_json(path=IPV4_GRAMMAR_PATH)
IPV6_GRAMMAR = load_grammar_from_json(path=IPV6_GRAMMAR_PATH)
JSON_AST_GRAMMAR = load_ast_grammar_from_json(path=JSON_AST_GRAMMAR_PATH)
IP_AST_GRAMMAR = load_ast_grammar_from_json(path=IP_AST_GRAMMAR_PATH)
IPV4_AST_GRAMMAR = load_ast_grammar_from_json(path=IPV4_AST_GRAMMAR_PATH)
IPV6_AST_GRAMMAR = load_ast_grammar_from_json(path=IPV6_AST_GRAMMAR_PATH)

_DEFAULT_GRAMMARS: dict[str, GrammarSpec] = {
    "json": JSON_GRAMMAR,
    "ip": IP_GRAMMAR,
    "ipv4": IPV4_GRAMMAR,
    "ipv6": IPV6_GRAMMAR,
    "grammar": JSON_GRAMMAR,
}
_ACTIVE_GRAMMARS: dict[str, GrammarSpec] = dict(_DEFAULT_GRAMMARS)
_DEFAULT_AST_GRAMMARS: dict[str, AstGrammarSpec] = {
    "json": JSON_AST_GRAMMAR,
    "ip": IP_AST_GRAMMAR,
    "ipv4": IPV4_AST_GRAMMAR,
    "ipv6": IPV6_AST_GRAMMAR,
    "grammar": JSON_AST_GRAMMAR,
}
_ACTIVE_AST_GRAMMARS: dict[str, AstGrammarSpec] = dict(_DEFAULT_AST_GRAMMARS)
_RUNTIME_GRAMMAR_VERSION = 0

__all__ = [
    "JSON_GRAMMAR_PATH",
    "IP_GRAMMAR_PATH",
    "IPV4_GRAMMAR_PATH",
    "IPV6_GRAMMAR_PATH",
    "JSON_AST_GRAMMAR_PATH",
    "IP_AST_GRAMMAR_PATH",
    "IPV4_AST_GRAMMAR_PATH",
    "IPV6_AST_GRAMMAR_PATH",
    "JSON_GRAMMAR",
    "IP_GRAMMAR",
    "IPV4_GRAMMAR",
    "IPV6_GRAMMAR",
    "JSON_AST_GRAMMAR",
    "IP_AST_GRAMMAR",
    "IPV4_AST_GRAMMAR",
    "IPV6_AST_GRAMMAR",
    "grammar_capabilities",
    "normalize_grammar_spec",
    "normalize_ast_grammar_spec",
    "grammar_spec_to_ast_grammar_spec",
    "load_grammar_from_json",
    "load_ast_grammar_from_json",
    "configure_runtime_grammar",
    "resolve_grammar_spec",
    "resolve_ast_grammar_spec",
    "runtime_grammar_version",
    "generate_from_grammar",
    "_expand_symbol_with_production_index",
]
