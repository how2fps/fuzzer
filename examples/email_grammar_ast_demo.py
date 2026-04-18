from __future__ import annotations

import random
import sys
from pathlib import Path

EXAMPLES_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXAMPLES_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mutator.versions import grammar_ast


RULES_PATH = EXAMPLES_DIR / "email_rules.txt"
START_RULE = "email_start"


def _rule_lines() -> list[str]:
    return [
        line.rstrip()
        for line in RULES_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _coverage_items(text: str) -> list[str]:
    return sorted(
        item
        for item in grammar_ast.coverage_items_for_text(text=text)
        if item.startswith(("production:", "site:", "textshape:"))
    )


def _one_visible_mutation(seed: str, *, rng_seed: int) -> str:
    for attempt in range(40):
        candidate = grammar_ast.mutate_from_rule(
            seed,
            start_rule=START_RULE,
            rng=random.Random(rng_seed + attempt),
            min_mutation_rounds=1,
            max_mutation_rounds=3,
            blend_with_seed=False,
        )
        if candidate != seed:
            return candidate
    return seed


def _print_box(title: str, lines: list[str]) -> None:
    width = max(len(title), *(len(line) for line in lines), 1)
    border = "+" + "-" * (width + 2) + "+"
    print(border)
    print(f"| {title.ljust(width)} |")
    print(border)
    for line in lines:
        print(f"| {line.ljust(width)} |")
    print(border)


def _print_rows(title: str, headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]
    border = "+" + "+".join("-" * (width + 2) for width in widths) + "+"
    print(f"\n{title}")
    print(border)
    print(
        "| "
        + " | ".join(headers[index].ljust(widths[index]) for index in range(len(headers)))
        + " |"
    )
    print(border)
    for row in rows:
        print(
            "| "
            + " | ".join(row[index].ljust(widths[index]) for index in range(len(headers)))
            + " |"
        )
    print(border)


def main() -> None:
    grammar_ast.configure(grammar_rules_file=str(RULES_PATH))

    print("\nEmail Grammar AST Demo")
    print("======================")
    print(f"Rules file: {RULES_PATH}")
    print("Start rule: email_start")
    _print_box("Grammar rules", _rule_lines())

    generated = grammar_ast.generate_from_rule(
        start_rule=START_RULE,
        rng=random.Random(7),
        count=8,
        min_mutation_rounds=0,
        max_mutation_rounds=0,
    )
    _print_rows(
        "1) Seedless generation",
        ("#", "Generated email seed", "Coverage highlights"),
        [
            (
                f"{index:02d}",
                sample,
                ", ".join(_coverage_items(sample)[:3]),
            )
            for index, sample in enumerate(generated, start=1)
        ],
    )

    targets = ["production:domain:1", "production:tld:5"]
    targeted_rows: list[tuple[str, str, str, str]] = []
    for item in targets:
        targeted = grammar_ast.generate_from_rule(
            start_rule=START_RULE,
            rng=random.Random(sum(ord(char) for char in item)),
            count=1,
            min_mutation_rounds=0,
            max_mutation_rounds=0,
            preferred_coverage_items=[item],
        )
        sample = targeted[0] if targeted else "<no candidate>"
        hit = item in grammar_ast.coverage_items_for_text(text=sample)
        targeted_rows.append((item, sample, str(hit), ", ".join(_coverage_items(sample)[:5])))
    _print_rows(
        "2) Coverage-targeted generation",
        ("Target coverage item", "Generated seed", "Hit?", "Observed coverage"),
        targeted_rows,
    )

    demo_seeds = [
        "alice@example.com",
        "bob_42@openai.net",
        "qa-team@docs.io",
        "z9@a-b.com",
        "dev_7@lab.ai",
    ]
    mutation_rows: list[tuple[str, str, str, str]] = []
    for index, seed in enumerate(demo_seeds, start=1):
        parsed = grammar_ast.parse_from_rule(text=seed, start_rule=START_RULE)
        mutated = _one_visible_mutation(seed, rng_seed=1000 + index)
        coverage = _coverage_items(seed)
        mutation_rows.append(
            (
                seed,
                str(parsed is not None),
                mutated,
                ", ".join(coverage[:4]),
            )
        )
    _print_rows(
        "3) Exact parse + grammar-aware mutation",
        ("Original seed", "Parsed?", "Mutated candidate", "Coverage highlights"),
        mutation_rows,
    )

    print("\nTakeaway")
    print("--------")
    print("Only examples/email_rules.txt defines the new email language.")
    print("The existing grammar_ast code handles generation, coverage labels, parsing, and mutation.")


if __name__ == "__main__":
    main()
