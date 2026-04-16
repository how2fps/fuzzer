from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass
from typing import Sequence

from core.fuzzer_logging import get_fuzzer_logger
from mutator.versions import grammar_ast


@dataclass(frozen=True)
class GrammarRefillResult:
    seeds: tuple[str, ...]
    observed_coverage_items: tuple[str, ...]
    uncovered_coverage_items: tuple[str, ...]


def _coverage_item_rule_name(item: str) -> str | None:
    if item.startswith("rule:") and len(item) > len("rule:"):
        return item[len("rule:"):]
    if item.startswith("production:"):
        parts = item.split(":", 2)
        if len(parts) == 3 and parts[1]:
            return parts[1]
    if item.startswith("site:"):
        parts = item.split(":")
        if len(parts) == 6 and parts[4]:
            return parts[4]
    if item.startswith("depth:"):
        parts = item.split(":", 2)
        if len(parts) == 3 and parts[1]:
            return parts[1]
    if item.startswith("repeat:"):
        parts = item.split(":", 4)
        if len(parts) == 5 and parts[1]:
            return parts[1]
    if item.startswith("charclass:"):
        parts = item.split(":", 4)
        if len(parts) == 5 and parts[1]:
            return parts[1]
    if item.startswith("number:"):
        parts = item.split(":", 4)
        if len(parts) == 5 and parts[1]:
            return parts[1]
    return None


def collect_history_texts(
    *,
    conn: sqlite3.Connection,
    target: str,
    ready_texts: Sequence[str] = (),
) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []

    def _record(text: str) -> None:
        if not text or text in seen:
            return
        seen.add(text)
        ordered.append(text)

    for text in ready_texts:
        _record(text)

    rows = conn.execute(
        """
        SELECT seed_text, mutated_input
        FROM runs
        WHERE target = ?
        ORDER BY id DESC
        """,
        (target,),
    )
    for seed_text, mutated_input in rows:
        _record(str(mutated_input or ""))
        _record(str(seed_text or ""))

    return ordered


def generate_grammar_refill_seeds(
    *,
    history_texts: Sequence[str],
    ready_texts: Sequence[str] = (),
    mutator_kind: str,
    rng: random.Random,
    count: int,
    debug_label: str | None = None,
) -> GrammarRefillResult:
    if count <= 0:
        return GrammarRefillResult(seeds=(), observed_coverage_items=(), uncovered_coverage_items=())

    available_items = tuple(grammar_ast.available_coverage_items(mutator_kind=mutator_kind))
    if not available_items:
        return GrammarRefillResult(seeds=(), observed_coverage_items=(), uncovered_coverage_items=())

    log = get_fuzzer_logger() if debug_label else None
    progress_interval = max(1, count // 8)

    observed_items: set[str] = set()
    known_texts: set[str] = set()
    coverage_counts: dict[str, int] = {item: 0 for item in available_items}
    available_set = set(available_items)

    def _record_coverage(text: str) -> None:
        if not text or text in known_texts:
            return
        known_texts.add(text)
        covered_items = grammar_ast.coverage_items_for_text(
            text=text,
            mutator_kind=mutator_kind,
        )
        tracked_items = covered_items & available_set
        for item in tracked_items:
            coverage_counts[item] = coverage_counts.get(item, 0) + 1
        observed_items.update(tracked_items)

    for text in ready_texts:
        _record_coverage(text)
    for text in history_texts:
        _record_coverage(text)
        if len(observed_items) >= len(available_items):
            break

    uncovered_items = tuple(item for item in available_items if item not in observed_items)
    grammar = grammar_ast.build(mutator_kind=mutator_kind)
    start_rule = grammar.start_rule or next(iter(grammar.rules))

    selected_seeds: list[str] = []
    generated_items: set[str] = set()
    seen_candidates: set[str] = set(known_texts)
    def _pick_best_candidate(*, focus_items: Sequence[str]) -> tuple[str | None, set[str], int]:
        if not focus_items:
            return None, set(), 0
        best_text: str | None = None
        best_items: set[str] = set()
        best_score = (-1, -1, -1)
        attempt_budget = max(24, min(160, len(focus_items) * 10))
        covered_so_far = (observed_items | generated_items) & available_set

        for attempt in range(attempt_budget):
            focus_item = (
                focus_items[attempt % len(focus_items)]
                if attempt < len(focus_items)
                else rng.choice(list(focus_items))
            )
            focus_rule = _coverage_item_rule_name(focus_item)
            generated = grammar_ast.generate_from_rule(
                start_rule=start_rule,
                rng=rng,
                count=1,
                min_mutation_rounds=0,
                max_mutation_rounds=0,
                preferred_rule_names=[focus_rule] if focus_rule else None,
                preferred_coverage_items=[focus_item],
                mutator_kind=mutator_kind,
            )
            if not generated:
                continue
            candidate = generated[0]
            if not candidate or candidate in seen_candidates:
                continue
            candidate_items = grammar_ast.coverage_items_for_text(
                text=candidate,
                mutator_kind=mutator_kind,
            )
            if not candidate_items:
                continue
            tracked_candidate_items = candidate_items & available_set
            new_items = tracked_candidate_items - covered_so_far
            score = (
                len(new_items),
                len(tracked_candidate_items & set(focus_items)),
                len(tracked_candidate_items),
            )
            if best_text is None or score > best_score:
                best_text = candidate
                best_items = candidate_items
                best_score = score
                if focus_item in new_items:
                    break

        return best_text, best_items, attempt_budget

    if log is not None:
        log.info(
            "%s: scanning %s history texts and %s ready texts for %s grammar seeds.",
            debug_label,
            len(history_texts),
            len(ready_texts),
            count,
        )

    while len(selected_seeds) < count:
        effective_counts = {
            item: coverage_counts.get(item, 0) + (1 if item in generated_items else 0)
            for item in available_items
        }
        min_count = min(effective_counts.values()) if effective_counts else 0
        low_coverage_items = [
            item for item in available_items if effective_counts.get(item, 0) == min_count
        ]
        # Prefer the lowest-coverage nodes first; fall back to all items if needed.
        focus_items = low_coverage_items
        best_text, best_items, attempt_budget = _pick_best_candidate(
            focus_items=focus_items
        )
        if best_text is None and len(low_coverage_items) != len(available_items):
            focus_items = sorted(
                available_items,
                key=lambda item: (effective_counts.get(item, 0), item),
            )
            best_text, best_items, attempt_budget = _pick_best_candidate(
                focus_items=focus_items
            )
        if best_text is None:
            if log is not None:
                log.warning(
                    "%s: hit grammar refill retry limit after %s attempts while searching for seed %s/%s (%s focus items remaining).",
                    debug_label,
                    attempt_budget,
                    len(selected_seeds) + 1,
                    count,
                    len(focus_items),
                )
            break
        selected_seeds.append(best_text)
        seen_candidates.add(best_text)
        generated_items.update(best_items)
        for item in best_items & available_set:
            coverage_counts[item] = coverage_counts.get(item, 0) + 1
        if log is not None and (
            len(selected_seeds) == 1
            or len(selected_seeds) == count
            or len(selected_seeds) % progress_interval == 0
        ):
            uncovered_remaining = sum(
                1
                for item in available_items
                if item not in observed_items and item not in generated_items
            )
            log.info(
                "%s: generated %s/%s grammar seeds (%s uncovered grammar items remaining).",
                debug_label,
                len(selected_seeds),
                count,
                uncovered_remaining,
            )

    return GrammarRefillResult(
        seeds=tuple(selected_seeds),
        observed_coverage_items=tuple(item for item in available_items if item in observed_items),
        uncovered_coverage_items=tuple(uncovered_items),
    )


__all__ = [
    "GrammarRefillResult",
    "collect_history_texts",
    "generate_grammar_refill_seeds",
]
