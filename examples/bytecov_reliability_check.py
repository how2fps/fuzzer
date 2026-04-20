#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from parser.parser import run_parser
from parser.pyc_coverage_runner import run_pyc_coverage


DEFAULT_INPUTS = {
    "ipv4-parser": ("1.1.1.", "1.1.1.1"),
    "ipv6-parser": ("1:1:1:", "1:1:1:1:1:1:1:1"),
    "cidrize-runner": ("1.1.1.", "1.1.1.1"),
}


def _coverage_lines(payload: Mapping[str, Any]) -> dict[str, tuple[int, ...]]:
    result: dict[str, tuple[int, ...]] = {}
    for entry in payload.get("branch_details_by_file") or []:
        if not isinstance(entry, Mapping):
            continue
        file_name = str(entry.get("file") or "")
        lines = entry.get("covered_lines") or []
        result[file_name] = tuple(sorted(int(line) for line in lines))
    return result


def _coverage_edges(payload: Mapping[str, Any]) -> dict[str, tuple[tuple[int, int], ...]]:
    result: dict[str, tuple[tuple[int, int], ...]] = {}
    for entry in payload.get("branch_details_by_file") or []:
        if not isinstance(entry, Mapping):
            continue
        file_name = str(entry.get("file") or "")
        edges = []
        for edge in entry.get("covered_branches") or []:
            if not isinstance(edge, Mapping):
                continue
            edges.append((int(edge.get("from_line") or 0), int(edge.get("to_line") or 0)))
        result[file_name] = tuple(sorted(edges))
    return result


def _signature(payload: Mapping[str, Any]) -> dict[str, Any]:
    bug = payload.get("bug_signature")
    bug_type = bug.get("type") if isinstance(bug, Mapping) else None
    bug_message = bug.get("message") if isinstance(bug, Mapping) else None
    return {
        "status": payload.get("status"),
        "bug_type": bug_type,
        "bug_message": bug_message,
        "covered_lines": int(payload.get("covered_lines") or 0),
        "covered_edges": int(payload.get("covered_edges") or 0),
        "covered_branches": int(payload.get("covered_branches") or 0),
        "lines_by_file": _coverage_lines(payload),
        "edges_by_file": _coverage_edges(payload),
    }


def _format_lines(lines_by_file: Mapping[str, tuple[int, ...]]) -> str:
    if not lines_by_file:
        return "none"
    parts = []
    for file_name, lines in sorted(lines_by_file.items()):
        rendered = ", ".join(str(line) for line in lines) if lines else "none"
        parts.append(f"{file_name}: {rendered}")
    return " | ".join(parts)


def _format_counts(sig: Mapping[str, Any]) -> str:
    return (
        f"lines={sig.get('covered_lines', 0)}, "
        f"edges={sig.get('covered_edges', 0)}, "
        f"branches={sig.get('covered_branches', 0)}"
    )


def _format_behavior(sig: Mapping[str, Any]) -> str:
    status = str(sig.get("status") or "unknown")
    bug_type = sig.get("bug_type")
    bug_message = sig.get("bug_message")
    if bug_type:
        return f"{status} ({bug_type}: {bug_message})"
    return status


def _print_summary(
    *,
    target: str,
    bad_input: str,
    good_input: str,
    bad_1: Mapping[str, Any],
    bad_2: Mapping[str, Any],
    good: Mapping[str, Any],
    shadow_sig: Mapping[str, Any] | None,
    checks: list[tuple[str, bool]],
) -> None:
    print("Byte Coverage Reliability Check")
    print("=" * 32)
    print(f"Target: {target}")
    print()
    print("Question 1: Does the same input give the same byte coverage?")
    print(f"  Input: {bad_input!r}")
    print(f"  Run #1: {_format_behavior(bad_1)} | {_format_counts(bad_1)}")
    print(f"          {_format_lines(bad_1.get('lines_by_file') or {})}")
    print(f"  Run #2: {_format_behavior(bad_2)} | {_format_counts(bad_2)}")
    print(f"          {_format_lines(bad_2.get('lines_by_file') or {})}")
    print()
    print("Question 2: Does a different execution path change byte coverage?")
    print(f"  Bad input : {bad_input!r}  -> {_format_counts(bad_1)}")
    print(f"  Good input: {good_input!r} -> {_format_counts(good)}")
    print(f"  Good lines: {_format_lines(good.get('lines_by_file') or {})}")
    if shadow_sig is not None:
        print()
        print("Question 3: Does the real parser/fuzzer path report the same bytecov?")
        print(f"  Direct bytecov : {_format_counts(bad_1)}")
        print(f"  shadow_result  : {_format_counts(shadow_sig)}")
        print(f"  Shadow lines   : {_format_lines(shadow_sig.get('lines_by_file') or {})}")
    print()
    print("Verdict")
    failed = False
    for label, ok in checks:
        mark = "PASS" if ok else "FAIL"
        print(f"  {mark}: {label}")
        failed = failed or not ok
    print()
    if failed:
        print("Result: byte coverage failed at least one reliability check.")
    elif shadow_sig is None:
        print("Result: byte coverage is deterministic and path-sensitive.")
    else:
        print("Result: byte coverage is deterministic, path-sensitive, and wired into shadow_result.")


def _print_json_details(
    *,
    target: str,
    bad_input: str,
    good_input: str,
    bad_1: Mapping[str, Any],
    bad_2: Mapping[str, Any],
    good: Mapping[str, Any],
    shadow_sig: Mapping[str, Any] | None,
    checks: list[tuple[str, bool]],
) -> None:
    print(f"target: {target}")
    print(f"bad input : {bad_input!r}")
    print(f"good input: {good_input!r}")
    print()
    print("bad run #1:")
    print(json.dumps(bad_1, indent=2, sort_keys=True))
    print()
    print("bad run #2:")
    print(json.dumps(bad_2, indent=2, sort_keys=True))
    print()
    print("good run:")
    print(json.dumps(good, indent=2, sort_keys=True))
    if shadow_sig is not None:
        print()
        print("parser shadow_result for bad input:")
        print(json.dumps(shadow_sig, indent=2, sort_keys=True))
    print()
    print("checks:")
    for label, ok in checks:
        mark = "PASS" if ok else "FAIL"
        print(f"  {mark} {label}")


def _run_bytecov(target: str, text: str, parser_config: dict[str, Any]) -> dict[str, Any]:
    return run_pyc_coverage(
        target_name=target,
        input_data=text.encode("utf-8"),
        parser_config=parser_config,
    )


def _run_parser_shadow(target: str, text: str, parser_config: dict[str, Any]) -> dict[str, Any]:
    result = run_parser(
        input_data=text.encode("utf-8"),
        target=target,
        timeout=10,
        print_json=False,
        enable_open_coverage=True,
        enable_qemu_coverage=False,
        enable_pyc_coverage=True,
        parser_config=parser_config,
    )
    shadow = result.get("shadow_result") if isinstance(result, Mapping) else None
    if not isinstance(shadow, Mapping):
        raise RuntimeError("parser did not return shadow_result bytecov payload")
    return dict(shadow)


def _parser_config_from_args(args: argparse.Namespace) -> dict[str, Any]:
    if not args.pyc_root and not args.module:
        return {}
    entry: dict[str, Any] = {}
    if args.pyc_root:
        entry["pyc_root"] = str(args.pyc_root)
    if args.module:
        entry["module"] = args.module
    return {"pyc_coverage": {"targets": {args.target: entry}}}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check that pyc/byte coverage is deterministic and input-sensitive."
    )
    parser.add_argument(
        "--target",
        choices=sorted(DEFAULT_INPUTS),
        default="ipv4-parser",
    )
    parser.add_argument("--bad-input", help="Input expected to take the buggy/error path.")
    parser.add_argument("--good-input", help="Input expected to take the valid path.")
    parser.add_argument("--pyc-root", type=Path, help="Optional extracted .pyc root override.")
    parser.add_argument("--module", help="Optional module override for the selected target.")
    parser.add_argument(
        "--skip-parser-shadow",
        action="store_true",
        help="Only test direct pyc coverage, not parser integration.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full raw signatures instead of the presentation summary.",
    )
    args = parser.parse_args()

    default_bad, default_good = DEFAULT_INPUTS[args.target]
    bad_input = args.bad_input if args.bad_input is not None else default_bad
    good_input = args.good_input if args.good_input is not None else default_good
    parser_config = _parser_config_from_args(args)

    bad_1 = _signature(_run_bytecov(args.target, bad_input, parser_config))
    bad_2 = _signature(_run_bytecov(args.target, bad_input, parser_config))
    good = _signature(_run_bytecov(args.target, good_input, parser_config))

    checks: list[tuple[str, bool]] = [
        ("same bad input is deterministic", bad_1 == bad_2),
        ("bad and good inputs have different bytecov signatures", bad_1 != good),
    ]

    shadow_sig = None
    if not args.skip_parser_shadow:
        shadow_sig = _signature(_run_parser_shadow(args.target, bad_input, parser_config))
        checks.append(("parser shadow_result matches direct bytecov", shadow_sig == bad_1))

    failed = False
    for label, ok in checks:
        failed = failed or not ok
    if args.json:
        _print_json_details(
            target=args.target,
            bad_input=bad_input,
            good_input=good_input,
            bad_1=bad_1,
            bad_2=bad_2,
            good=good,
            shadow_sig=shadow_sig,
            checks=checks,
        )
    else:
        _print_summary(
            target=args.target,
            bad_input=bad_input,
            good_input=good_input,
            bad_1=bad_1,
            bad_2=bad_2,
            good=good,
            shadow_sig=shadow_sig,
            checks=checks,
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
