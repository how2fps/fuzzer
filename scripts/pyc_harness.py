#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from parser.pyc_coverage_runner import run_pyc_coverage


TARGET_MAP = {
    "cidrize": "cidrize-runner",
    "ipv4": "ipv4-parser",
    "ipv6": "ipv6-parser",
}


def _build_parser_config(target_name: str, pyc_root: Path | None) -> dict[str, object]:
    if pyc_root is None:
        return {}
    return {
        "pyc_coverage": {
            "targets": {
                target_name: {
                    "pyc_root": str(pyc_root),
                }
            }
        }
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Minimal harness to run extracted .pyc modules.")
    parser.add_argument("--target", choices=sorted(TARGET_MAP), required=True)
    parser.add_argument(
        "--pyc-root",
        type=Path,
        help="Override pyc root (defaults to extracted_pyc/<target>/pyc).",
    )
    parser.add_argument(
        "--coverage",
        action="store_true",
        help="Enable minimal line/edge/branch coverage reporting.",
    )
    parser.add_argument("--input", dest="input_text", help="Input string to parse.")
    parser.add_argument("--input-file", type=Path, help="Path to file to read input from.")
    args = parser.parse_args()

    if args.input_file is not None:
        input_text = args.input_file.read_text(encoding="utf-8", errors="replace")
    elif args.input_text is not None:
        input_text = args.input_text
    else:
        input_text = sys.stdin.read()
    input_text = input_text.strip()

    target_name = TARGET_MAP[args.target]
    parser_config = _build_parser_config(target_name, args.pyc_root)
    payload = run_pyc_coverage(
        target_name=target_name,
        input_data=input_text.encode("utf-8"),
        parser_config=parser_config or None,
    )

    status = payload.get("status", "error")
    bug = payload.get("bug_signature")
    if status == "bug" and isinstance(bug, dict):
        error_text = bug.get("message") or ""
        error_prefix = bug.get("type") or "Error"
        print(f"status=bug error={error_prefix}: {error_text}")
    else:
        print(f"status={status}")

    if args.coverage:
        covered_lines = int(payload.get("covered_lines") or 0)
        total_lines = int(payload.get("total_lines") or 0)
        covered_branches = int(payload.get("covered_branches") or 0)
        total_branches = int(payload.get("total_branches") or 0)
        covered_edges = int(payload.get("covered_edges") or 0)
        total_edges = int(payload.get("total_edges") or 0)

        def _fmt_cov(covered: int, total: int) -> str:
            if total <= 0:
                return f"{covered} / n/a"
            ratio = (covered / float(total)) * 100.0
            return f"{covered} / {total} ({ratio:.1f}%)"

        print("coverage.statement:", _fmt_cov(covered_lines, total_lines))
        print("coverage.branch   :", _fmt_cov(covered_branches, total_branches))
        print("coverage.edge     :", _fmt_cov(covered_edges, total_edges))

    return 0 if status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
