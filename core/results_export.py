from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from core.db_utils import get_inputs_for_unique_error_line_pairs
from core.fuzzer_logging import get_fuzzer_logger
from core.sqlite_conn import open_results_db
from core.target_artifacts import copy_bug_counts_csv_if_present

_VALIDITY_FORMAT_BY_TARGET = {
    "json-decoder": "json",
    "json_open": "json",
    "ipv4-parser": "ipv4",
    "ipv6-parser": "ipv6",
}


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _dedupe_rows_by_file_bugtype_line(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Keep one row per (file, bug_type, line), preferring the lowest iteration.
    If iteration is missing for both, keep the first seen row.
    """
    best_by_key: dict[tuple[str, str, str], tuple[tuple[int, int, int], dict[str, Any]]] = {}
    for idx, row in enumerate(rows):
        key = (
            str(row.get("file") or row.get("filename") or "").strip(),
            str(row.get("bug_type") or "").strip(),
            str(row.get("line") or row.get("lineno") or "").strip(),
        )
        iteration = _safe_int(row.get("iteration"))
        rank = (
            1 if iteration is None else 0,
            iteration if iteration is not None else 10**18,
            idx,
        )
        existing = best_by_key.get(key)
        if existing is None or rank < existing[0]:
            best_by_key[key] = (rank, row)

    # Preserve stable output ordering by original encounter index.
    return [row for _, row in sorted(best_by_key.values(), key=lambda t: t[0][2])]


def _short_exc(exc: str | None) -> str:
    s = (exc or "").strip()
    if not s:
        return ""
    return s.split(".")[-1].strip()


def _pick_shortest_input(current: str | None, candidate: Any) -> str:
    text = "" if candidate is None else str(candidate)
    if current is None or len(text) < len(current):
        return text
    return current


def _build_db_match_index(conn) -> dict[tuple[str, int, str], dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT exception, line, file, bug_type,
               seed_id, seed_text, mutated_input, status, iteration, isinteresting_score,
               created_at
        FROM runs
        WHERE status IN ('bug', 'crash', 'timeout') AND file IS NOT NULL AND line IS NOT NULL
        ORDER BY created_at, iteration
        """
    )
    out: dict[tuple[str, int, str], dict[str, Any]] = {}
    for row in cur.fetchall():
        (
            exc,
            line,
            file_,
            _bug_type,
            seed_id,
            seed_text,
            mutated_input,
            status,
            iteration,
            score,
            created_at,
        ) = row
        line_int = _safe_int(line)
        file_norm = str(file_ or "").strip()
        exc_norm = _short_exc(exc)
        if line_int is None or not file_norm or not exc_norm:
            continue
        key = (file_norm, line_int, exc_norm)
        existing = out.get(key)
        shortest_input = _pick_shortest_input(
            existing.get("shortest_input") if existing is not None else None,
            mutated_input,
        )
        if existing is not None:
            existing["shortest_input"] = shortest_input
            continue
        out[key] = {
            "exception": exc,
            "line": line_int,
            "file": file_norm,
            "seed_id": seed_id,
            "seed_text": seed_text,
            "mutated_input": mutated_input,
            "shortest_input": shortest_input,
            "status": status,
            "iteration": iteration,
            "isinteresting_score": score,
            "datetime_executed": created_at,
        }
    return out


def _export_run_charts(*, results_folder: Path, target: str) -> None:
    runs_csv = results_folder / "runs.csv"
    if not runs_csv.is_file():
        return

    log = get_fuzzer_logger()
    try:
        from analyze_runs_validity import (
            build_binned_validity_table,
            build_cumulative_coverage_table,
            default_coverage_output_path,
            default_output_path,
            load_runs_dataframe,
            plot_coverage_chart,
            plot_validity_chart,
        )

        df = load_runs_dataframe(runs_csv)

        coverage_summary = build_cumulative_coverage_table(df)
        coverage_output = default_coverage_output_path(runs_csv)
        plot_coverage_chart(
            coverage_summary,
            runs_csv=runs_csv,
            output_path=coverage_output,
        )
        coverage_summary.to_csv(coverage_output.with_suffix(".csv"), index=False)

        fmt = _VALIDITY_FORMAT_BY_TARGET.get(target)
        if fmt:
            validity_summary, bin_seconds = build_binned_validity_table(df, fmt=fmt)
            validity_output = default_output_path(runs_csv, fmt, "binned")
            plot_validity_chart(
                validity_summary,
                fmt=fmt,
                runs_csv=runs_csv,
                output_path=validity_output,
                mode="binned",
                bin_seconds=bin_seconds,
            )
            validity_summary.to_csv(validity_output.with_suffix(".csv"), index=False)
        else:
            log.info(
                "Skipping validity chart for target %s; no known validity format mapping.",
                target,
            )
    except Exception as exc:
        log.warning("Failed to generate charts for %s: %s", results_folder, exc)


def _export_pairs_from_bug_counts(
    *,
    conn,
    results_folder: Path,
) -> bool:
    bug_counts_path = results_folder / "bug_counts.csv"
    if not bug_counts_path.is_file():
        return False

    with open(bug_counts_path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        bug_fields = list(reader.fieldnames or [])
        bug_rows = list(reader)

    if not bug_fields:
        return False

    db_index = _build_db_match_index(conn)
    appended_fields = [
        "exception",
        "line",
        "file",
        "seed_id",
        "seed_text",
        "mutated_input",
        "shortest_input",
        "status",
        "iteration",
        "isinteresting_score",
        "datetime_executed",
    ]
    out_fields = [*bug_fields, *appended_fields]
    out_rows: list[dict[str, Any]] = []
    log = get_fuzzer_logger()

    for row in bug_rows:
        filename = str(row.get("filename") or "").strip()
        lineno = _safe_int(row.get("lineno"))
        exc_type = str(row.get("exc_type") or "").strip()
        key = (filename, lineno, exc_type) if lineno is not None else None

        out_row: dict[str, Any] = {k: row.get(k, "") for k in bug_fields}
        for field in appended_fields:
            out_row[field] = ""
        out_row["line"] = "" if lineno is None else lineno
        out_row["file"] = filename

        matched = db_index.get(key) if key is not None else None
        if matched is None:
            log.warning(
                "No DB match for bug_counts row (filename=%r, lineno=%r, exc_type=%r)",
                filename,
                row.get("lineno"),
                exc_type,
            )
        else:
            for field in appended_fields:
                out_row[field] = matched.get(field, "")
        out_rows.append(out_row)

    out_rows = _dedupe_rows_by_file_bugtype_line(out_rows)

    pairs_path = results_folder / "unique_error_line_pairs.csv"
    with open(pairs_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=out_fields)
        w.writeheader()
        w.writerows(out_rows)
    return True


def export_results(
    *,
    results_folder: Path,
    db_path: Path,
    target: str,
    copy_bug_counts: bool = True,
    parser_config: dict[str, Any] | None = None,
) -> None:
    get_fuzzer_logger().info("Exporting results to %s", results_folder)
    if copy_bug_counts:
        copy_bug_counts_csv_if_present(
            target=target,
            results_folder=results_folder,
            parser_config=parser_config,
        )

    conn = open_results_db(db_path)
    try:
        exported_from_bug_counts = _export_pairs_from_bug_counts(
            conn=conn,
            results_folder=results_folder,
        )
        if not exported_from_bug_counts:
            pairs = get_inputs_for_unique_error_line_pairs(conn)
            pairs_path = results_folder / "unique_error_line_pairs.csv"
            if pairs:
                pairs = _dedupe_rows_by_file_bugtype_line(pairs)
                with open(pairs_path, "w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(
                        f,
                        fieldnames=[
                            "exception",
                            "line",
                            "file",
                            "bug_type",
                            "seed_id",
                            "seed_text",
                            "mutated_input",
                            "shortest_input",
                            "status",
                            "iteration",
                            "isinteresting_score",
                            "datetime_executed",
                        ],
                    )
                    w.writeheader()
                    w.writerows(pairs)
            else:
                with open(pairs_path, "w", newline="", encoding="utf-8") as f:
                    f.write(
                        "exception,line,file,bug_type,seed_id,seed_text,mutated_input,"
                        "shortest_input,"
                        "status,iteration,isinteresting_score,datetime_executed\n"
                    )

        runs_path = results_folder / "runs.csv"
        cur = conn.execute(
            "SELECT iteration, seed_id, seed_text, mutated_input, generation_time_seconds, "
            "run_time_seconds, status, bug_type, exception, message, file, line, "
            "isinteresting_score, unique_covered_arcs, covered_branches, target, created_at "
            "FROM runs"
        )
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        with open(runs_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(rows)
        _export_run_charts(results_folder=results_folder, target=target)
    finally:
        conn.close()

