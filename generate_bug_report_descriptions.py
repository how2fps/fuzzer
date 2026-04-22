from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path


TARGET_DISPLAY_NAMES = {
    "cidrize-runner": "Cidrize",
    "ipv4-parser": "IPv4 Parser",
    "ipv6-parser": "IPv6 Parser",
    "json-decoder": "JSON Decoder",
}


def _resolve_csv_field_size_limit() -> int:
    original_limit = csv.field_size_limit()
    candidate = sys.maxsize
    try:
        while candidate > 0:
            try:
                csv.field_size_limit(candidate)
                return candidate
            except OverflowError:
                candidate //= 10
    finally:
        csv.field_size_limit(original_limit)
    raise OverflowError("Unable to determine a usable CSV field size limit")


CSV_FIELD_SIZE_LIMIT = _resolve_csv_field_size_limit()


def _ensure_large_csv_field_limit() -> None:
    if csv.field_size_limit() < CSV_FIELD_SIZE_LIMIT:
        csv.field_size_limit(CSV_FIELD_SIZE_LIMIT)


def _load_csv_rows(path: Path) -> list[dict[str, str]]:
    _ensure_large_csv_field_limit()
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _safe_int(value: str | None) -> int:
    if value is None:
        return sys.maxsize
    text = str(value).strip()
    if not text:
        return sys.maxsize
    try:
        return int(text)
    except ValueError:
        try:
            return int(float(text))
        except ValueError:
            return sys.maxsize


def _first_nonempty(*values: str | None) -> str:
    for value in values:
        text = str(value or "")
        if text:
            return text
    return ""


def _get_target_name(runs_rows: list[dict[str, str]], run_folder: Path) -> str:
    for row in runs_rows:
        target = str(row.get("target") or "").strip()
        if target:
            return target
    folder_name = run_folder.parent.name
    for target in TARGET_DISPLAY_NAMES:
        if folder_name.startswith(target):
            return target
    return folder_name


def _target_display_name(target: str) -> str:
    if target in TARGET_DISPLAY_NAMES:
        return TARGET_DISPLAY_NAMES[target]
    return target.replace("-", " ").title()


def _bug_title(row: dict[str, str]) -> str:
    return _first_nonempty(
        row.get("exc_message"),
        row.get("message"),
        row.get("exception"),
        row.get("exc_type"),
        "Unknown bug",
    )


def _bug_file(row: dict[str, str]) -> str:
    return _first_nonempty(row.get("file"), row.get("filename"), "unknown_file")


def _bug_line(row: dict[str, str]) -> str:
    return _first_nonempty(row.get("line"), row.get("lineno"), "unknown_line")


def _escape_literal(value: str) -> str:
    escaped: list[str] = []
    for ch in value:
        if ch == "\n":
            escaped.append("\\n")
        elif ch == "\r":
            escaped.append("\\r")
        elif ch == "\t":
            escaped.append("\\t")
        elif ord(ch) < 32 or ord(ch) == 127:
            escaped.append(f"\\x{ord(ch):02x}")
        else:
            escaped.append(ch)
    return "".join(escaped)


def _format_inline_code(value: str) -> str:
    literal = _escape_literal(value)
    if literal == "":
        return "``"
    max_backtick_run = 0
    current_run = 0
    for ch in literal:
        if ch == "`":
            current_run += 1
            max_backtick_run = max(max_backtick_run, current_run)
        else:
            current_run = 0
    fence = "`" * (max_backtick_run + 1)
    needs_padding = literal.startswith("`") or literal.endswith("`")
    if needs_padding:
        return f"{fence} {literal} {fence}"
    return f"{fence}{literal}{fence}"


def _format_prose_literal(value: str) -> str:
    return _escape_literal(value)


def _truncate_display(value: str, *, limit: int = 240) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def _bash_quote(value: str) -> str:
    if value == "":
        return "''"
    if all(32 <= ord(ch) <= 126 and ch != "'" for ch in value):
        return "'" + value + "'"

    escaped: list[str] = []
    for ch in value:
        if ch == "\\":
            escaped.append("\\\\")
        elif ch == "'":
            escaped.append("\\'")
        elif ch == "\n":
            escaped.append("\\n")
        elif ch == "\r":
            escaped.append("\\r")
        elif ch == "\t":
            escaped.append("\\t")
        elif ord(ch) < 32 or ord(ch) == 127:
            escaped.append(f"\\x{ord(ch):02x}")
        elif ord(ch) <= 126:
            escaped.append(ch)
        elif ord(ch) <= 0xFFFF:
            escaped.append(f"\\u{ord(ch):04x}")
        else:
            escaped.append(f"\\U{ord(ch):08x}")
    return "$'" + "".join(escaped) + "'"


def _reproduction_command(*, target: str, shortest_input: str) -> str:
    quoted_input = _bash_quote(shortest_input)
    if target == "cidrize-runner":
        return f"cidrize-runner --func cidrize --ipstr {quoted_input} --raise-errors"
    if target == "ipv4-parser":
        return f"ipv4-parser --ipstr {quoted_input}"
    if target == "ipv6-parser":
        return f"ipv6-parser --ipstr {quoted_input}"
    if target == "json-decoder":
        return f"uv run json_decoder_stv.py --str-json {quoted_input}"
    return f"{target} {quoted_input}"


def _find_shortest_input_row(
    shortest_input: str,
    runs_rows: list[dict[str, str]],
) -> dict[str, str] | None:
    matches = [
        row
        for row in runs_rows
        if str(row.get("mutated_input") or "") == shortest_input
    ]
    if not matches:
        return None
    return min(matches, key=lambda row: _safe_int(row.get("iteration")))


def _is_exact_reproduction(
    unique_row: dict[str, str],
    matched_run_row: dict[str, str],
) -> bool:
    return (
        str(unique_row.get("exception") or "").strip()
        == str(matched_run_row.get("exception") or "").strip()
        and str(unique_row.get("exc_message") or "").strip()
        == str(matched_run_row.get("message") or "").strip()
        and _bug_file(unique_row).strip() == str(matched_run_row.get("file") or "").strip()
        and _bug_line(unique_row).strip() == str(matched_run_row.get("line") or "").strip()
    )


def _verification_line(
    *,
    unique_row: dict[str, str],
    matched_run_row: dict[str, str] | None,
) -> str:
    if matched_run_row is None:
        return "- Verification against `runs.csv`: No exact match. The shortest input was not found as a `mutated_input` entry in `runs.csv`."

    iteration = str(matched_run_row.get("iteration") or "unknown")
    matched_file = _first_nonempty(matched_run_row.get("file"), "unknown_file")
    matched_line = _first_nonempty(matched_run_row.get("line"), "unknown_line")
    if _is_exact_reproduction(unique_row, matched_run_row):
        return (
            "- Verification against `runs.csv`: Yes. "
            f"The shortest input appears in iteration `{iteration}` "
            f"and reproduces the same bug at `{matched_file}:{matched_line}`."
        )

    matched_message = _first_nonempty(
        matched_run_row.get("message"),
        matched_run_row.get("exception"),
        "Unknown result",
    )
    matched_message = _truncate_display(matched_message)
    return (
        "- Verification against `runs.csv`: No exact match. "
        f"The shortest input appears in iteration `{iteration}`, "
        f"but it produced {_format_inline_code(matched_message)} "
        f"at `{matched_file}:{matched_line}` instead."
    )


def generate_report(run_folder: Path) -> str:
    unique_path = run_folder / "unique_error_line_pairs.csv"
    runs_path = run_folder / "runs.csv"
    unique_rows = _load_csv_rows(unique_path)
    runs_rows = _load_csv_rows(runs_path)

    target = _get_target_name(runs_rows, run_folder)
    display_target = _target_display_name(target)
    unique_path_link = f"{unique_path.resolve()}:1"
    runs_path_link = f"{runs_path.resolve()}:1"

    parts = [
        f"# {display_target} Bug Report Descriptions",
        "",
        (
            "These reports are derived from "
            f"[unique_error_line_pairs.csv]({unique_path_link}) "
            "and cross-checked against "
            f"[runs.csv]({runs_path_link})."
        ),
        "",
    ]

    for index, row in enumerate(unique_rows, start=1):
        title = _bug_title(row)
        file_name = _bug_file(row)
        line = _bug_line(row)
        bug_type = _first_nonempty(row.get("bug_type"), "unknown")
        exc_type = _first_nonempty(row.get("exc_type"), "unknown")
        shortest_input = str(row.get("shortest_input") or "")
        sample_input = str(row.get("mutated_input") or "")
        matched_run_row = _find_shortest_input_row(shortest_input, runs_rows)
        command = _reproduction_command(target=target, shortest_input=shortest_input)

        parts.extend(
            [
                f"Bug Title: {title}",
                "",
                "2. Bug Description:",
                f"Bug type: {bug_type}",
                f"Exception type: {exc_type}",
                f"The bug was recorded at {file_name}:{line}",
                "",
                (
                    "3. Reproduction Steps: "
                    f"Run the ‘{target}’ target with the input "
                    f"‘{_format_prose_literal(shortest_input)}’."
                ),
                command,
                "",
                "4. Proof of Concept (PoC):",
                f"- Shortest input from the minimization result: {_format_inline_code(shortest_input)}.",
                f"- Original sample input from the unique bug row: {_format_inline_code(sample_input)}.",
                "",
                "5. Attachments:",
                f"- [unique_error_line_pairs.csv]({unique_path_link})",
                f"- [runs.csv]({runs_path_link})",
                _verification_line(unique_row=row, matched_run_row=matched_run_row),
                "",
            ]
        )

    return "\n".join(parts).rstrip() + "\n"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate bug_report_descriptions.md for one or more run folders "
            "that contain unique_error_line_pairs.csv and runs.csv."
        )
    )
    parser.add_argument(
        "run_folders",
        nargs="+",
        type=Path,
        help="Run folder(s) to process.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    for run_folder in args.run_folders:
        resolved_folder = run_folder.resolve()
        unique_path = resolved_folder / "unique_error_line_pairs.csv"
        runs_path = resolved_folder / "runs.csv"
        if not unique_path.is_file():
            raise FileNotFoundError(f"Missing file: {unique_path}")
        if not runs_path.is_file():
            raise FileNotFoundError(f"Missing file: {runs_path}")
        output_path = resolved_folder / "bug_report_descriptions.md"
        output_path.write_text(generate_report(resolved_folder), encoding="utf-8")
        print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
