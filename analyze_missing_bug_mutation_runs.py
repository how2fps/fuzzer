from __future__ import annotations

import argparse
import csv
import json
import math
import random
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from mutator import configure_runtime_grammar
from mutator.mutator import (
    apply_grammar_operator,
    available_grammar_operator_names,
    resolve_grammar_spec,
)


ATTRIBUTE_ERROR_DID_YOU_MEAN_RE = re.compile(r"\. Did you mean: .*?\?$")
TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[^A-Za-z0-9]+")
CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
WHITESPACE_RE = re.compile(r"\s+")
NON_ALNUM_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")


@dataclass(frozen=True)
class BaselineBug:
    row_index: int
    raw: dict[str, str]
    file: str
    line: str
    exception: str
    message: str
    bug_type: str
    input_text: str


@dataclass(frozen=True)
class RunRecord:
    run_db: Path
    iteration: int
    seed_id: str
    seed_text: str
    mutated_input: str
    status: str
    bug_type: str
    exception: str
    message: str
    file: str
    line: str


@dataclass
class SearchState:
    text: str
    source_text: str
    steps: int
    score: float
    path: tuple[str, ...]


@dataclass(frozen=True)
class MutationEstimate:
    found_exact: bool
    estimated_operator_steps: int
    estimated_mutation_runs: int
    source_text: str
    best_text: str
    best_score: float
    operator_path: tuple[str, ...]
    lower_bound_steps: int


def _normalize_exception(exc: str) -> str:
    value = (exc or "").strip()
    if not value:
        return ""
    return value.split(".")[-1]


def _normalize_message(message: str) -> str:
    value = (message or "").strip()
    if not value:
        return ""
    value = ATTRIBUTE_ERROR_DID_YOU_MEAN_RE.sub("", value)
    value = WHITESPACE_RE.sub(" ", value).strip()
    return value


def _signature_key(*, file: str, line: str, exception: str, message: str) -> tuple[str, str, str, str]:
    return (
        (file or "").strip(),
        (line or "").strip(),
        _normalize_exception(exception),
        _normalize_message(message),
    )


def _line_exception_key(*, file: str, line: str, exception: str) -> tuple[str, str, str]:
    return (
        (file or "").strip(),
        (line or "").strip(),
        _normalize_exception(exception),
    )


def _infer_mutator_kind(*, mutator_kind: str, target: str, grammar_path: str | None) -> str:
    if grammar_path is not None:
        return "grammar"
    target_lower = (target or "").lower()
    if "json" in target_lower:
        return "json"
    if "ipv4" in target_lower or "ipv6" in target_lower or "cidr" in target_lower:
        return "ip"
    return "grammar"


def _iter_run_dbs(results_path: Path) -> list[Path]:
    if results_path.is_file() and results_path.name == "runs.db":
        return [results_path]
    if not results_path.exists():
        raise FileNotFoundError(results_path)
    return sorted(path for path in results_path.rglob("runs.db") if path.is_file())


def _load_baseline(csv_path: Path) -> list[BaselineBug]:
    with csv_path.open(newline="", encoding="utf-8", errors="replace") as handle:
        reader = csv.DictReader(handle)
        rows: list[BaselineBug] = []
        for idx, row in enumerate(reader, start=1):
            rows.append(
                BaselineBug(
                    row_index=idx,
                    raw={str(k): "" if v is None else str(v) for k, v in row.items()},
                    file=str(row.get("file") or "").strip(),
                    line=str(row.get("line") or "").strip(),
                    exception=str(row.get("exception") or "").strip(),
                    message=str(row.get("message") or "").strip(),
                    bug_type=str(row.get("bug_type") or "").strip(),
                    input_text=str(row.get("input") or ""),
                )
            )
    return rows


def _load_runs(run_dbs: list[Path]) -> list[RunRecord]:
    runs: list[RunRecord] = []
    for db_path in run_dbs:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            for row in conn.execute(
                """
                SELECT
                    iteration,
                    COALESCE(seed_id, '') AS seed_id,
                    COALESCE(seed_text, '') AS seed_text,
                    COALESCE(mutated_input, '') AS mutated_input,
                    COALESCE(status, '') AS status,
                    COALESCE(bug_type, '') AS bug_type,
                    COALESCE(exception, '') AS exception,
                    COALESCE(message, '') AS message,
                    COALESCE(file, '') AS file,
                    COALESCE(line, '') AS line
                FROM runs
                ORDER BY iteration
                """
            ):
                try:
                    iteration = int(row["iteration"])
                except (TypeError, ValueError):
                    iteration = 0
                runs.append(
                    RunRecord(
                        run_db=db_path,
                        iteration=iteration,
                        seed_id=str(row["seed_id"]),
                        seed_text=str(row["seed_text"]),
                        mutated_input=str(row["mutated_input"]),
                        status=str(row["status"]),
                        bug_type=str(row["bug_type"]),
                        exception=str(row["exception"]),
                        message=str(row["message"]),
                        file=str(row["file"]),
                        line=str(row["line"]),
                    )
                )
        finally:
            conn.close()
    runs.sort(key=lambda row: (str(row.run_db), row.iteration, row.seed_id, row.mutated_input))
    return runs


def _load_config_for_run_db(run_db: Path) -> dict[str, Any]:
    config_path = run_db.with_name("config.json")
    if not config_path.is_file():
        raise FileNotFoundError(f"Could not find sibling config.json for {run_db}")
    with config_path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _get_nested_config_value(config: dict[str, Any], section: str, key: str, default: Any = None) -> Any:
    nested = config.get(section)
    if isinstance(nested, dict) and key in nested:
        return nested[key]
    return config.get(key, default)


def _extract_start_texts(
    *,
    runs: list[RunRecord],
    config: dict[str, Any],
    start_set: str,
) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []

    def remember(text: str) -> None:
        if text in seen:
            return
        seen.add(text)
        out.append(text)

    if start_set == "observed-seeds":
        for row in runs:
            if row.seed_text:
                remember(row.seed_text)
        return out

    if start_set == "observed-inputs":
        for row in runs:
            if row.mutated_input:
                remember(row.mutated_input)
        return out

    seed_corpus_version = str(_get_nested_config_value(config, "seed_corpus", "seed_corpus_version", ""))
    if seed_corpus_version == "regex-noseed":
        for row in runs:
            if row.seed_id.startswith("regex-") and row.seed_text:
                remember(row.seed_text)
        return out

    for row in runs:
        if not row.seed_text:
            continue
        if row.seed_text != row.mutated_input:
            continue
        if row.seed_id.startswith("discovered-") or row.seed_id.startswith("generated-"):
            continue
        remember(row.seed_text)
    return out


def _tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall(text)


def _wordish_tokens(text: str) -> list[str]:
    return [token for token in NON_ALNUM_SPLIT_RE.split(text) if token]


def _looks_numericish(token: str) -> bool:
    if not token:
        return False
    if token.isdigit():
        return True
    lowered = token.lower()
    if lowered in {"nan", "inf", "infinity"}:
        return True
    return all(ch in "abcdefABCDEF" for ch in token)


def _separator_multiset(text: str) -> Counter[str]:
    return Counter(ch for ch in text if not ch.isalnum())


def _edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i]
        for j, char_b in enumerate(b, start=1):
            replace_cost = previous[j - 1] + (0 if char_a == char_b else 1)
            insert_cost = current[j - 1] + 1
            delete_cost = previous[j] + 1
            current.append(min(replace_cost, insert_cost, delete_cost))
        previous = current
    return previous[-1]


def _similarity_score(candidate: str, target: str) -> float:
    if candidate == target:
        return 1_000_000.0

    char_ratio = SequenceMatcher(None, candidate, target).ratio()
    candidate_tokens = _wordish_tokens(candidate)
    target_tokens = _wordish_tokens(target)
    token_ratio = SequenceMatcher(None, candidate_tokens, target_tokens).ratio()
    prefix_len = len(_common_prefix(candidate, target))
    prefix_ratio = prefix_len / max(1, min(len(candidate), len(target)))
    length_penalty = abs(len(candidate) - len(target)) / max(1, len(target))
    edit_penalty = _edit_distance(candidate[:160], target[:160]) / max(1, min(160, len(target)))
    return (
        (char_ratio * 0.6)
        + (token_ratio * 0.35)
        + (prefix_ratio * 0.1)
        - (length_penalty * 0.2)
        - (edit_penalty * 0.1)
    )


def _common_prefix(a: str, b: str) -> str:
    limit = min(len(a), len(b))
    idx = 0
    while idx < limit and a[idx] == b[idx]:
        idx += 1
    return a[:idx]


def _operator_sample_count(operator_name: str, target: str, base_samples: int) -> int:
    boosted = {
        "token_whitespace_surgery": any(ch.isspace() for ch in target),
        "insert_control_burst": bool(CONTROL_CHAR_RE.search(target)),
        "alphabetic_label_substitution": any(ch.isalpha() for ch in target),
        "separator_run_surgery": "--" in target or "//" in target or "[[" in target,
        "repetition_amplification_surgery": len(target) >= 48,
        "numeric_format_surgery": any(ch.isdigit() for ch in target),
        "extreme_numeric_surgery": any(ch.isdigit() for ch in target),
        "structured_range_surgery": "-" in target,
        "descending_range_surgery": "-" in target,
        "embed_alternative_fragment": any(ch.isalpha() for ch in target),
        "cross_branch_alternation_splice": any(ch.isalpha() for ch in target),
        "trailing_or_leading_extra_data": bool(target[:1].isspace() or target[-1:].isspace()),
    }
    samples = max(1, base_samples)
    if boosted.get(operator_name, False):
        samples += 1
    if operator_name == "regenerate":
        return min(samples, 2)
    return samples


def _seed_from_parts(*parts: str) -> int:
    seed = 0x9E3779B97F4A7C15
    for part in parts:
        for char in part:
            seed ^= ord(char)
            seed = (seed * 0x100000001B3) & ((1 << 63) - 1)
    return seed


def _lower_bound_steps(source: str, target: str) -> int:
    if source == target:
        return 0

    source_tokens = _wordish_tokens(source)
    target_tokens = _wordish_tokens(target)
    numeric_diffs = 0
    alpha_needed = False
    whitespace_needed = False
    control_needed = False

    for src, dst in zip(source_tokens, target_tokens):
        if src == dst:
            continue
        if _looks_numericish(src) and _looks_numericish(dst):
            numeric_diffs += 1
        elif any(ch.isalpha() for ch in dst):
            alpha_needed = True

    if len(target_tokens) > len(source_tokens):
        extra_tokens = target_tokens[len(source_tokens):]
        if extra_tokens:
            if any(any(ch.isalpha() for ch in token) for token in extra_tokens):
                alpha_needed = True
            if sum(1 for token in extra_tokens if _looks_numericish(token)) > 0:
                numeric_diffs += 1

    if any(ch.isspace() for ch in target) and not any(ch.isspace() for ch in source):
        whitespace_needed = True
    if CONTROL_CHAR_RE.search(target) and not CONTROL_CHAR_RE.search(source):
        control_needed = True

    source_separators = _separator_multiset(source)
    target_separators = _separator_multiset(target)
    separator_steps = 0
    for char in set(source_separators) | set(target_separators):
        diff = abs(target_separators[char] - source_separators[char])
        if diff:
            separator_steps += 1 if char.strip() else 0
            if char in ".:/-[]":
                separator_steps += 1
    if "--" in target and "--" not in source:
        separator_steps += 1
    if "//" in target and "//" not in source:
        separator_steps += 1

    length_growth_steps = 0
    if len(target) > len(source):
        ratio = len(target) / max(1, len(source))
        if ratio > 1.4:
            length_growth_steps = max(1, math.ceil(math.log2(ratio)))

    direct_edit_floor = max(1, math.ceil(_edit_distance(source, target) / 24))

    total = 0
    total += min(6, numeric_diffs)
    total += 1 if alpha_needed else 0
    total += 1 if whitespace_needed else 0
    total += 1 if control_needed else 0
    total += min(4, separator_steps)
    total += min(5, length_growth_steps)
    total = max(total, direct_edit_floor)
    return total


def _estimate_missing_bug_distance(
    *,
    target_text: str,
    start_texts: list[str],
    grammar_spec: dict[str, Any],
    operator_names: tuple[str, ...],
    chain_max_depth: int,
    beam_width: int,
    seed_limit: int,
    max_operator_steps: int,
    samples_per_operator: int,
    operator_max_depth: int,
) -> MutationEstimate:
    if not start_texts:
        return MutationEstimate(
            found_exact=False,
            estimated_operator_steps=max_operator_steps + 1,
            estimated_mutation_runs=max(1, math.ceil((max_operator_steps + 1) / max(1, chain_max_depth))),
            source_text="",
            best_text="",
            best_score=float("-inf"),
            operator_path=(),
            lower_bound_steps=max_operator_steps + 1,
        )

    ranked_start_texts = sorted(
        start_texts,
        key=lambda text: _similarity_score(text, target_text),
        reverse=True,
    )
    frontier: list[SearchState] = []
    best_completion_steps: int | None = None
    best_completion_state: SearchState | None = None
    visited_steps: dict[str, int] = {}

    for text in ranked_start_texts[: max(1, seed_limit)]:
        state = SearchState(
            text=text,
            source_text=text,
            steps=0,
            score=_similarity_score(text, target_text),
            path=(),
        )
        frontier.append(state)
        visited_steps[text] = 0
        completion_steps = _lower_bound_steps(text, target_text)
        if best_completion_steps is None or completion_steps < best_completion_steps:
            best_completion_steps = completion_steps
            best_completion_state = state
        if text == target_text:
            return MutationEstimate(
                found_exact=True,
                estimated_operator_steps=0,
                estimated_mutation_runs=0,
                source_text=text,
                best_text=text,
                best_score=state.score,
                operator_path=(),
                lower_bound_steps=0,
            )

    for step in range(1, max_operator_steps + 1):
        next_by_text: dict[str, SearchState] = {}
        for state in frontier:
            for operator_name in operator_names:
                sample_count = _operator_sample_count(operator_name, target_text, samples_per_operator)
                for sample_index in range(sample_count):
                    rng = random.Random(
                        _seed_from_parts(
                            state.text,
                            target_text,
                            operator_name,
                            str(step),
                            str(sample_index),
                        )
                    )
                    try:
                        candidate = apply_grammar_operator(
                            operator_name=operator_name,
                            original_text=state.text,
                            grammar_spec=grammar_spec,
                            max_depth=operator_max_depth,
                            rng=rng,
                        )
                    except Exception:
                        continue
                    if not candidate or candidate == state.text:
                        continue
                    previous = visited_steps.get(candidate)
                    if previous is not None and previous <= step:
                        continue
                    visited_steps[candidate] = step
                    candidate_state = SearchState(
                        text=candidate,
                        source_text=state.source_text,
                        steps=step,
                        score=_similarity_score(candidate, target_text),
                        path=(*state.path, operator_name),
                    )
                    incumbent = next_by_text.get(candidate)
                    if incumbent is None or candidate_state.score > incumbent.score:
                        next_by_text[candidate] = candidate_state
                    completion_steps = step + _lower_bound_steps(candidate, target_text)
                    if best_completion_steps is None or completion_steps < best_completion_steps:
                        best_completion_steps = completion_steps
                        best_completion_state = candidate_state
                    if candidate == target_text:
                        return MutationEstimate(
                            found_exact=True,
                            estimated_operator_steps=step,
                            estimated_mutation_runs=math.ceil(step / max(1, chain_max_depth)),
                            source_text=state.source_text,
                            best_text=candidate,
                            best_score=candidate_state.score,
                            operator_path=candidate_state.path,
                            lower_bound_steps=step,
                        )

        frontier = sorted(
            next_by_text.values(),
            key=lambda state: (state.score, -state.steps, -len(state.path)),
            reverse=True,
        )[: max(1, beam_width)]
        if not frontier:
            break

    if best_completion_state is None or best_completion_steps is None:
        best_completion_steps = max_operator_steps + 1
        best_completion_state = SearchState(
            text="",
            source_text="",
            steps=0,
            score=float("-inf"),
            path=(),
        )
    return MutationEstimate(
        found_exact=False,
        estimated_operator_steps=best_completion_steps,
        estimated_mutation_runs=math.ceil(best_completion_steps / max(1, chain_max_depth)),
        source_text=best_completion_state.source_text,
        best_text=best_completion_state.text,
        best_score=best_completion_state.score,
        operator_path=best_completion_state.path,
        lower_bound_steps=best_completion_steps,
    )


def _compare_counts(
    *,
    baseline: list[BaselineBug],
    runs: list[RunRecord],
) -> dict[str, int]:
    exact_inputs = {row.mutated_input for row in runs}
    signature_keys = {
        _signature_key(
            file=row.file,
            line=row.line,
            exception=row.exception,
            message=row.message,
        )
        for row in runs
        if row.status == "bug"
    }
    line_exception_keys = {
        _line_exception_key(file=row.file, line=row.line, exception=row.exception)
        for row in runs
        if row.status == "bug"
    }

    exact_input_matches = sum(1 for bug in baseline if bug.input_text in exact_inputs)
    signature_matches = sum(
        1
        for bug in baseline
        if _signature_key(
            file=bug.file,
            line=bug.line,
            exception=bug.exception,
            message=bug.message,
        )
        in signature_keys
    )
    line_exception_matches = sum(
        1
        for bug in baseline
        if _line_exception_key(file=bug.file, line=bug.line, exception=bug.exception)
        in line_exception_keys
    )
    return {
        "baseline_total": len(baseline),
        "exact_input_matches": exact_input_matches,
        "signature_matches": signature_matches,
        "file_line_exception_matches": line_exception_matches,
    }


def _select_missing_bugs(
    *,
    baseline: list[BaselineBug],
    runs: list[RunRecord],
    match_mode: str,
) -> list[BaselineBug]:
    exact_inputs = {row.mutated_input for row in runs}
    signature_keys = {
        _signature_key(
            file=row.file,
            line=row.line,
            exception=row.exception,
            message=row.message,
        )
        for row in runs
        if row.status == "bug"
    }
    line_exception_keys = {
        _line_exception_key(file=row.file, line=row.line, exception=row.exception)
        for row in runs
        if row.status == "bug"
    }

    out: list[BaselineBug] = []
    for bug in baseline:
        if match_mode == "exact-input":
            matched = bug.input_text in exact_inputs
        elif match_mode == "file-line-exception":
            matched = (
                _line_exception_key(file=bug.file, line=bug.line, exception=bug.exception)
                in line_exception_keys
            )
        else:
            matched = (
                _signature_key(
                    file=bug.file,
                    line=bug.line,
                    exception=bug.exception,
                    message=bug.message,
                )
                in signature_keys
            )
        if not matched:
            out.append(bug)
    return out


def _write_output_csv(output_path: Path, rows: list[dict[str, Any]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        fields = [
            "row_index",
            "bug_type",
            "file",
            "line",
            "exception",
            "message",
            "input",
            "search_status",
            "estimated_operator_steps",
            "estimated_mutation_runs",
            "best_start_text",
            "best_candidate_text",
            "best_candidate_score",
            "operator_path",
        ]
    else:
        fields = list(rows[0].keys())
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare a fuzzing batch against a baseline bug CSV and estimate how many "
            "mutation runs are needed to reach the missing trigger inputs."
        )
    )
    parser.add_argument("results_path", type=Path, help="Batch directory or a specific runs.db")
    parser.add_argument("baseline_csv", type=Path, help="Baseline bug CSV to compare against")
    parser.add_argument(
        "--match-mode",
        choices=("signature", "file-line-exception", "exact-input"),
        default="signature",
        help=(
            "How to decide whether a baseline bug was already found. "
            "'signature' uses file + line + short exception + normalized message."
        ),
    )
    parser.add_argument(
        "--start-set",
        choices=("startup", "observed-seeds", "observed-inputs"),
        default="startup",
        help="What reachable texts to treat as the starting frontier for the estimator",
    )
    parser.add_argument(
        "--beam-width",
        type=int,
        default=8,
        help="How many candidate states to keep per operator-depth",
    )
    parser.add_argument(
        "--seed-limit",
        type=int,
        default=6,
        help="How many start texts to seed the search with",
    )
    parser.add_argument(
        "--max-operator-steps",
        type=int,
        default=4,
        help="Maximum mutator operator applications to explore per missing bug",
    )
    parser.add_argument(
        "--samples-per-operator",
        type=int,
        default=1,
        help="Base number of deterministic samples to try for each operator",
    )
    parser.add_argument(
        "--operator-max-depth",
        type=int,
        default=5,
        help="Max grammar depth passed to apply_grammar_operator",
    )
    parser.add_argument(
        "--output-csv",
        type=Path,
        default=None,
        help="Optional CSV path for the missing-bug distance report",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help="Optional JSON path for the combined summary and missing-bug rows",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_argument_parser()
    args = parser.parse_args(argv)

    run_dbs = _iter_run_dbs(args.results_path.resolve())
    if not run_dbs:
        parser.error(f"No runs.db files found under {args.results_path}")

    config = _load_config_for_run_db(run_dbs[0])
    runs = _load_runs(run_dbs)
    baseline = _load_baseline(args.baseline_csv.resolve())

    target = str(config.get("target") or "")
    mutator_kind = str(_get_nested_config_value(config, "mutator", "mutator_kind", "auto"))
    grammar_path = _get_nested_config_value(config, "mutator", "grammar_path")
    ast_grammar_path = _get_nested_config_value(config, "mutator", "ast_grammar_path")
    chain_max_depth = int(_get_nested_config_value(config, "mutator", "mutation_chain_max_depth", 8) or 8)
    effective_mutator = _infer_mutator_kind(
        mutator_kind=mutator_kind,
        target=target,
        grammar_path=grammar_path,
    )

    configure_runtime_grammar(
        kind=effective_mutator,
        grammar_path=grammar_path,
        ast_grammar_path=ast_grammar_path,
    )
    grammar_spec = resolve_grammar_spec(kind=effective_mutator)
    operator_names = available_grammar_operator_names(grammar_spec=grammar_spec)

    summary = _compare_counts(baseline=baseline, runs=runs)
    missing_bugs = _select_missing_bugs(
        baseline=baseline,
        runs=runs,
        match_mode=args.match_mode,
    )
    start_texts = _extract_start_texts(
        runs=runs,
        config=config,
        start_set=args.start_set,
    )

    report_rows: list[dict[str, Any]] = []
    for bug in missing_bugs:
        estimate = _estimate_missing_bug_distance(
            target_text=bug.input_text,
            start_texts=start_texts,
            grammar_spec=grammar_spec,
            operator_names=operator_names,
            chain_max_depth=chain_max_depth,
            beam_width=max(1, args.beam_width),
            seed_limit=max(1, args.seed_limit),
            max_operator_steps=max(1, args.max_operator_steps),
            samples_per_operator=max(1, args.samples_per_operator),
            operator_max_depth=max(1, args.operator_max_depth),
        )
        report_rows.append(
            {
                "row_index": bug.row_index,
                "bug_type": bug.bug_type,
                "file": bug.file,
                "line": bug.line,
                "exception": bug.exception,
                "message": bug.message,
                "input": bug.input_text,
                "search_status": "found_exact" if estimate.found_exact else "heuristic_lower_bound",
                "estimated_operator_steps": estimate.estimated_operator_steps,
                "estimated_mutation_runs": estimate.estimated_mutation_runs,
                "best_start_text": estimate.source_text,
                "best_candidate_text": estimate.best_text,
                "best_candidate_score": f"{estimate.best_score:.6f}",
                "operator_path": " > ".join(estimate.operator_path),
                "lower_bound_steps": estimate.lower_bound_steps,
            }
        )

    print(
        json.dumps(
            {
                "results_path": str(args.results_path.resolve()),
                "baseline_csv": str(args.baseline_csv.resolve()),
                "run_db_count": len(run_dbs),
                "target": target,
                "effective_mutator": effective_mutator,
                "match_mode": args.match_mode,
                "start_set": args.start_set,
                "chain_max_depth": chain_max_depth,
                "startup_or_start_text_count": len(start_texts),
                "comparison_summary": summary,
                "missing_bug_count": len(missing_bugs),
            },
            indent=2,
            sort_keys=True,
        )
    )

    if report_rows:
        print("\nMissing bug estimates:")
        for row in report_rows:
            print(
                f"- row {row['row_index']}: {row['file']}:{row['line']} "
                f"{row['exception']} | runs~{row['estimated_mutation_runs']} "
                f"(steps~{row['estimated_operator_steps']}, status={row['search_status']})"
            )
            print(f"  input={row['input']!r}")
            if row["best_start_text"]:
                print(f"  start={row['best_start_text']!r}")
            if row["operator_path"]:
                print(f"  path={row['operator_path']}")
            if row["best_candidate_text"] and row["best_candidate_text"] != row["input"]:
                print(f"  best_candidate={row['best_candidate_text']!r}")

    if args.output_csv is not None:
        _write_output_csv(args.output_csv.resolve(), report_rows)

    if args.output_json is not None:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(
            json.dumps(
                {
                    "summary": {
                        "results_path": str(args.results_path.resolve()),
                        "baseline_csv": str(args.baseline_csv.resolve()),
                        "run_db_count": len(run_dbs),
                        "target": target,
                        "effective_mutator": effective_mutator,
                        "match_mode": args.match_mode,
                        "start_set": args.start_set,
                        "chain_max_depth": chain_max_depth,
                        "startup_or_start_text_count": len(start_texts),
                        "comparison_summary": summary,
                        "missing_bug_count": len(missing_bugs),
                    },
                    "missing_bug_estimates": report_rows,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
