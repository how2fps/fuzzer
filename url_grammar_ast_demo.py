from __future__ import annotations

import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mutator.versions import grammar_ast


def _mutate_demo_seed_for_rule(seed: str, start_rule: str) -> tuple[bool, str]:
    """Show exact parsing and find one visible mutation for a demo seed/rule pair."""
    parsed = grammar_ast.parse_from_rule(text=seed, start_rule=start_rule) is not None
    mutated = seed
    seed_bias = sum(ord(char) for char in seed)
    for attempt in range(20):
        candidate = grammar_ast.mutate_from_rule(
            seed,
            start_rule=start_rule,
            rng=random.Random(seed_bias + attempt),
            blend_with_seed=False,
        )
        if candidate != seed:
            mutated = candidate
            break
    return parsed, mutated


def _print_table(title: str, rows: list[tuple[str, str, str]]) -> None:
    """Print a simple fixed-width table for easy terminal screenshots."""
    seed_width = max(len("Seed"), *(len(seed) for seed, _, _ in rows))
    parsed_width = len("Parsed")
    mutated_width = max(len("Mutated"), *(len(mutated) for _, _, mutated in rows))

    border = (
        "+"
        + "-" * (seed_width + 2)
        + "+"
        + "-" * (parsed_width + 2)
        + "+"
        + "-" * (mutated_width + 2)
        + "+"
    )

    print(f"\n{title}")
    print(border)
    print(
        f"| {'Seed'.ljust(seed_width)} | {'Parsed'.ljust(parsed_width)} | "
        f"{'Mutated'.ljust(mutated_width)} |"
    )
    print(border)
    for seed, parsed, mutated in rows:
        print(
            f"| {seed.ljust(seed_width)} | {parsed.ljust(parsed_width)} | "
            f"{mutated.ljust(mutated_width)} |"
        )
    print(border)


def main() -> None:
    rng = random.Random(42)
    rules_path = REPO_ROOT / "configs" / "examples" / "url_rules.txt"

    grammar_ast.configure(grammar_rules_file=str(rules_path))

    print("Generated URL-shaped samples:")
    for sample in grammar_ast.generate_from_rule(
        start_rule="url_start",
        rng=rng,
        count=5,
    ):
        print("  ", sample)

    print("\nGenerated email-shaped samples:")
    for sample in grammar_ast.generate_from_rule(
        start_rule="email_start",
        rng=random.Random(99),
        count=5,
    ):
        print("  ", sample)

    url_demo_seeds = [
        "https://openai.com/docs?id=42",
        "http://a-b.net/x1",
        "https://x1.y2/path_7?id=9",
        "http://docs.openai.com/api/v1?id=2048",
        "https://abc-1.xy/docs_more/part-2?id=73",
        "http://x9.y8/a_b-c",
        "https://k9.z8/docs",
        "http://aa-bb.cc/path_1?id=7",
        "https://r2d2.ai/api_2",
        "http://m-n.op/q",
    ]

    email_demo_seeds = [
        "alice@example.com",
        "bob_42@openai.net",
        "sam.dev@x1-y2.org",
        "qa-team@docs.io",
        "z9@a-b.co",
        "dev_7@lab.ai",
        "user.name@alpha-beta.dev",
        "ops@infra.net",
        "a1_b2@x9-y8.org",
        "qa99@demo.co",
    ]

    url_rows: list[tuple[str, str, str]] = []
    for seed in url_demo_seeds:
        parsed, mutated = _mutate_demo_seed_for_rule(seed, "url_start")
        url_rows.append((seed, str(parsed), mutated))

    email_rows: list[tuple[str, str, str]] = []
    for seed in email_demo_seeds:
        parsed, mutated = _mutate_demo_seed_for_rule(seed, "email_start")
        email_rows.append((seed, str(parsed), mutated))

    _print_table("Exact parse + mutate demo: URLs", url_rows)
    _print_table("Exact parse + mutate demo: emails", email_rows)


if __name__ == "__main__":
    main()
