"""
Base interestingness: AFL-style scoring (status, differential, new covered
arcs from seen_branches DB, rare-bug from runs DB).
Seen_branches insertion is done by main, not here.
"""
from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from core.behavioral_signals import (
    build_differential_behavior,
    describe_input_structure,
    execution_stability_bonus,
    late_parse_depth_from_result,
    partial_parse_success,
)
from core.sqlite_conn import open_results_db


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    return None


def _normalize_status(status: Any) -> str:
    if not isinstance(status, str):
        return ""
    return status.strip().lower()


def _bug_signatures_equal(a: Any, b: Any) -> bool:
    ma = _as_mapping(a)
    mb = _as_mapping(b)
    if ma is None or mb is None:
        return ma is None and mb is None

    keys = ("type", "exception", "message", "file", "line")
    return all(ma.get(k) == mb.get(k) for k in keys)


def _status_score(*, closed_status: str) -> float:
    """
    Basic per-run interestingness based only on the closed_result status.
    """
    if not closed_status:
        return 0.0

    if closed_status in {"bug", "crash"}:
        return 0.9
    if closed_status in {"timeout"}:
        return 0.7
    if closed_status in {"error"}:
        return 0.6

    return 0.0


def _differential_score(
    *,
    closed_status: str,
    open_status: str | None,
    closed_bug: Mapping[str, Any] | None,
    open_bug: Mapping[str, Any] | None,
) -> float:
    """
    Score based on differences between closed and open (oracle) behavior.
    """
    if not open_status and not open_bug:
        return 0.0

    if open_status is None:
        open_status = ""

    # Strong signal: closed finds a problem while the oracle looks fine.
    if closed_status in {"bug", "crash", "timeout", "error"} and open_status == "ok":
        return 1.0

    # Status differs in any other way: still interesting but slightly less.
    if closed_status != open_status:
        return 0.75

    # Same status; check whether the detailed bug signatures disagree.
    if closed_status in {"bug", "crash", "error"} and not _bug_signatures_equal(
        closed_bug, open_bug
    ):
        return 0.5

    return 0.0


def get_covered_edges_from_result(result: Mapping[str, Any]) -> set[tuple[str, int, int]]:
    """Extract (file, from_line, to_line) for all covered branches. Used by main to insert into seen_branches."""
    closed_raw = result.get("closed_result") if isinstance(result, Mapping) else None
    closed = _as_mapping(closed_raw)
    if closed is None:
        return set()
    shadow_raw = result.get("shadow_result") if isinstance(result, Mapping) else None
    shadow_res = _as_mapping(shadow_raw) if shadow_raw is not None else None
    open_raw = result.get("open_result") if isinstance(result, Mapping) else None
    open_res = _as_mapping(open_raw) if open_raw is not None else None
    coverage_source = _select_coverage_source(
        closed=closed,
        shadow_res=shadow_res,
        open_res=open_res,
    )
    return _get_covered_edges(coverage_source)


def get_covered_lines_from_result(result: Mapping[str, Any]) -> set[tuple[str, int]]:
    """Extract (file, line) for all covered statements (line-based)."""
    closed_raw = result.get("closed_result") if isinstance(result, Mapping) else None
    closed = _as_mapping(closed_raw)
    if closed is None:
        return set()
    shadow_raw = result.get("shadow_result") if isinstance(result, Mapping) else None
    shadow_res = _as_mapping(shadow_raw) if shadow_raw is not None else None
    open_raw = result.get("open_result") if isinstance(result, Mapping) else None
    open_res = _as_mapping(open_raw) if open_raw is not None else None
    coverage_source = _select_coverage_source(
        closed=closed,
        shadow_res=shadow_res,
        open_res=open_res,
    )
    return _get_covered_lines(coverage_source)


def get_coverage_source_kind_from_result(result: Mapping[str, Any]) -> str:
    """Return `closed`, `shadow`, `open`, or `none` for the selected coverage source."""
    closed_raw = result.get("closed_result") if isinstance(result, Mapping) else None
    closed = _as_mapping(closed_raw)
    if closed is None:
        return "none"
    shadow_raw = result.get("shadow_result") if isinstance(result, Mapping) else None
    shadow_res = _as_mapping(shadow_raw) if shadow_raw is not None else None
    open_raw = result.get("open_result") if isinstance(result, Mapping) else None
    open_res = _as_mapping(open_raw) if open_raw is not None else None
    return _coverage_source_kind(
        closed=closed,
        shadow_res=shadow_res,
        open_res=open_res,
    )


def _has_coverage_fields(value: Mapping[str, Any] | None) -> bool:
    if value is None:
        return False
    covered = value.get("covered_branches")
    missing = value.get("missing_branches")
    details = value.get("branch_details_by_file")
    if covered is not None and missing is not None and isinstance(details, Sequence):
        return True
    return bool(_get_covered_edges(value))


def _coverage_source_kind(
    *,
    closed: Mapping[str, Any],
    shadow_res: Mapping[str, Any] | None,
    open_res: Mapping[str, Any] | None,
) -> str:
    if _has_coverage_fields(closed):
        return "closed"
    if _has_coverage_fields(shadow_res):
        return "shadow"
    if _has_coverage_fields(open_res):
        return "open"
    return "none"


def _select_coverage_source(
    *,
    closed: Mapping[str, Any],
    shadow_res: Mapping[str, Any] | None,
    open_res: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    source_kind = _coverage_source_kind(
        closed=closed,
        shadow_res=shadow_res,
        open_res=open_res,
    )
    if source_kind == "closed":
        return closed
    if source_kind == "shadow" and shadow_res is not None:
        return shadow_res
    if source_kind == "open" and open_res is not None:
        return open_res
    return closed


def _get_covered_edges(closed: Mapping[str, Any]) -> set[tuple[str, int, int]]:
    edges: set[tuple[str, int, int]] = set()
    details = closed.get("branch_details_by_file")
    if not isinstance(details, Sequence):
        return edges
    for file_entry in details:
        if not _as_mapping(file_entry):
            continue
        file_name = file_entry.get("file")
        if not file_name:
            continue
        covered_list = file_entry.get("covered_branches")
        if not isinstance(covered_list, Sequence):
            continue
        for arc in covered_list:
            arc_map = _as_mapping(arc) if arc is not None else None
            if arc_map is None:
                continue
            try:
                from_line = int(arc_map.get("from_line", 0))
                to_line = int(arc_map.get("to_line", 0))
            except (TypeError, ValueError):
                continue
            if from_line <= 0:
                continue
            edges.add((str(file_name), from_line, to_line))
    return edges


def _get_covered_branch_edges(source: Mapping[str, Any]) -> set[tuple[str, int, int]]:
    edges: set[tuple[str, int, int]] = set()
    details = source.get("branch_details_by_file")
    if not isinstance(details, Sequence):
        return edges
    for file_entry in details:
        if not _as_mapping(file_entry):
            continue
        file_name = file_entry.get("file")
        if not file_name:
            continue
        raw_list = file_entry.get("covered_branch_edges")
        if not isinstance(raw_list, Sequence):
            raw_list = file_entry.get("covered_branches")
        if not isinstance(raw_list, Sequence):
            continue
        for arc in raw_list:
            arc_map = _as_mapping(arc) if arc is not None else None
            if arc_map is None:
                continue
            try:
                from_line = int(arc_map.get("from_line", 0))
                to_line = int(arc_map.get("to_line", 0))
            except (TypeError, ValueError):
                continue
            if from_line <= 0:
                continue
            edges.add((str(file_name), from_line, to_line))
    return edges


def _get_covered_lines(source: Mapping[str, Any]) -> set[tuple[str, int]]:
    lines: set[tuple[str, int]] = set()
    details = source.get("branch_details_by_file")
    if not isinstance(details, Sequence):
        return lines
    for file_entry in details:
        if not _as_mapping(file_entry):
            continue
        file_name = file_entry.get("file")
        if not file_name:
            continue
        raw_lines = file_entry.get("covered_lines")
        if not isinstance(raw_lines, Sequence):
            continue
        for raw_line in raw_lines:
            try:
                line = int(raw_line)
            except (TypeError, ValueError):
                continue
            if line <= 0:
                continue
            lines.add((str(file_name), line))
    return lines


def _coverage_key_from_edges(edges: set[tuple[str, int, int]]) -> str:
    if not edges:
        return ""
    ordered = sorted(edges)
    raw = repr(ordered).encode("utf-8", errors="replace")
    return "COV:" + hashlib.sha256(raw).hexdigest()[:16]


def _new_edges_score(conn: sqlite3.Connection, edges: set[tuple[str, int, int]]) -> float:
    """Read-only: which result edges are absent from seen_branches; AFL-style score. No insert."""
    stats = _new_edges_stats(conn, edges)
    if stats is None:
        return 0.0
    new_count, edge_count = stats
    if edge_count <= 0:
        return 0.0
    new_ratio = new_count / float(edge_count)
    new_edge_presence = min(float(new_count), 1.0)
    return (0.5 * new_edge_presence) + (0.5 * min(new_ratio, 1.0))


def _new_edges_stats(
    conn: sqlite3.Connection,
    edges: set[tuple[str, int, int]],
) -> tuple[int, int] | None:
    """Return (new_edge_count, total_edge_count) for this input against seen_branches."""
    if not edges:
        return (0, 0)
    new_count = 0
    try:
        # Indexed lookups on PRIMARY KEY (file, from_line, to_line) — avoid full-table scans
        # that grew with corpus size and dominated worker time.
        for f, fl, tl in edges:
            row = conn.execute(
                "SELECT 1 FROM seen_branches WHERE file = ? AND from_line = ? AND to_line = ? LIMIT 1",
                (f, fl, tl),
            ).fetchone()
            if row is None:
                new_count += 1
    except sqlite3.OperationalError:
        return None
    return (new_count, len(edges))


def _new_lines_stats(
    conn: sqlite3.Connection,
    lines: set[tuple[str, int]],
) -> tuple[int, int] | None:
    if not lines:
        return (0, 0)
    new_count = 0
    try:
        for file_name, line in lines:
            row = conn.execute(
                "SELECT 1 FROM seen_lines WHERE file = ? AND line = ? LIMIT 1",
                (file_name, line),
            ).fetchone()
            if row is None:
                new_count += 1
    except sqlite3.OperationalError:
        return None
    return (new_count, len(lines))


def _coverage_backend_name(value: Mapping[str, Any] | None) -> str:
    if value is None:
        return ""
    backend = value.get("coverage_backend")
    if not isinstance(backend, str):
        return ""
    return backend.strip().lower()


def _edge_novelty_metrics(
    conn: sqlite3.Connection,
    *,
    target: str,
    edges: set[tuple[str, int, int]],
) -> tuple[int, float]:
    if not edges:
        return (0, 0.0)
    new_count = 0
    rare_total = 0.0
    try:
        for file_name, from_line, to_line in edges:
            seen_row = conn.execute(
                """
                SELECT 1
                FROM seen_branches
                WHERE file = ? AND from_line = ? AND to_line = ?
                LIMIT 1
                """,
                (file_name, from_line, to_line),
            ).fetchone()
            if seen_row is None:
                new_count += 1
            count_row = conn.execute(
                """
                SELECT hit_count
                FROM edge_observations
                WHERE target = ? AND file = ? AND from_line = ? AND to_line = ?
                LIMIT 1
                """,
                (target, file_name, from_line, to_line),
            ).fetchone()
            if count_row is None:
                if seen_row is None:
                    rare_total += 1.0
                continue
            hit_count = int(count_row[0])
            rare_total += 1.0 / (1.0 + float(hit_count))
    except sqlite3.OperationalError:
        return (0, 0.0)
    return (new_count, min(1.0, rare_total / float(len(edges))))


def _is_first_diff_behavior(
    conn: sqlite3.Connection,
    *,
    target: str,
    diff_pattern_key: str,
) -> bool:
    if not diff_pattern_key:
        return False
    try:
        row = conn.execute(
            """
            SELECT 1
            FROM runs
            WHERE target = ? AND COALESCE(diff_pattern_key, '') = COALESCE(?, '')
            LIMIT 1
            """,
            (target, diff_pattern_key),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is None


def _input_structure_novelty(
    conn: sqlite3.Connection,
    *,
    target: str,
    coverage_key: str,
    structure_key: str,
    length_bucket: str,
) -> float:
    if not structure_key:
        return 0.0

    def _exists(column: str, value: str) -> bool:
        row = conn.execute(
            f"""
            SELECT 1
            FROM runs
            WHERE target = ?
              AND COALESCE(coverage_key, '') = COALESCE(?, '')
              AND COALESCE({column}, '') = COALESCE(?, '')
            LIMIT 1
            """,
            (target, coverage_key, value),
        ).fetchone()
        return row is not None

    try:
        novelty = 0.0
        if not _exists("structure_key", structure_key):
            novelty += 0.7
        if length_bucket and not _exists("length_bucket", length_bucket):
            novelty += 0.3
        return min(1.0, novelty)
    except sqlite3.OperationalError:
        return 0.0


def _repeat_bug_count(
    conn: sqlite3.Connection,
    closed_status: str,
    closed_bug: Mapping[str, Any] | None,
    target: str,
) -> int | None:
    if closed_status not in {"bug", "crash", "timeout", "error"}:
        return None
    if not closed_bug:
        return None
    bug_type = closed_bug.get("type") or ""
    exc = closed_bug.get("exception") or ""
    file_ = closed_bug.get("file") or ""
    line_raw = closed_bug.get("line")
    line = None
    if line_raw is not None:
        try:
            line = int(line_raw)
        except (TypeError, ValueError):
            line = None
    try:
        cur = conn.execute(
            """
            SELECT COUNT(*) FROM runs
            WHERE target = ? AND status = ?
              AND COALESCE(bug_type, '') = COALESCE(?, '')
              AND COALESCE(exception, '') = COALESCE(?, '')
              AND COALESCE(file, '') = COALESCE(?, '')
              AND ((line IS NOT NULL AND line = ?) OR (line IS NULL AND ? IS NULL))
            """,
            (target, closed_status, bug_type, exc, file_, line, line),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0
    except sqlite3.OperationalError:
        return None


def _repeat_bug_factor(repeat_count: int | None) -> float:
    if repeat_count is None or repeat_count <= 0:
        return 1.0
    return 1.0 / (1.0 + repeat_count)


def _rare_bug_score(repeat_count: int | None) -> float:
    if repeat_count is None:
        return 0.0
    return 1.0 / (1.0 + repeat_count)


def _repeat_bug_site_count(
    conn: sqlite3.Connection,
    closed_status: str,
    closed_bug: Mapping[str, Any] | None,
    target: str,
) -> int | None:
    if closed_status not in {"bug", "crash", "timeout", "error"}:
        return None
    if not closed_bug:
        return None
    file_ = closed_bug.get("file") or ""
    line_raw = closed_bug.get("line")
    line = None
    if line_raw is not None:
        try:
            line = int(line_raw)
        except (TypeError, ValueError):
            line = None
    if not file_ and line is None:
        return None
    try:
        cur = conn.execute(
            """
            SELECT COUNT(*) FROM runs
            WHERE target = ? AND status IN ('bug', 'crash', 'timeout', 'error')
              AND COALESCE(file, '') = COALESCE(?, '')
              AND ((line IS NOT NULL AND line = ?) OR (line IS NULL AND ? IS NULL))
            """,
            (target, file_, line, line),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0
    except sqlite3.OperationalError:
        return None


def _repeat_exception_site_count(
    conn: sqlite3.Connection,
    closed_status: str,
    closed_bug: Mapping[str, Any] | None,
    target: str,
) -> int | None:
    if closed_status not in {"bug", "crash", "timeout", "error"}:
        return None
    if not closed_bug:
        return None
    exc = closed_bug.get("exception") or ""
    file_ = closed_bug.get("file") or ""
    line_raw = closed_bug.get("line")
    line = None
    if line_raw is not None:
        try:
            line = int(line_raw)
        except (TypeError, ValueError):
            line = None
    if not exc:
        return None
    try:
        cur = conn.execute(
            """
            SELECT COUNT(*) FROM runs
            WHERE target = ? AND status IN ('bug', 'crash', 'timeout', 'error')
              AND COALESCE(exception, '') = COALESCE(?, '')
              AND COALESCE(file, '') = COALESCE(?, '')
              AND ((line IS NOT NULL AND line = ?) OR (line IS NULL AND ? IS NULL))
            """,
            (target, exc, file_, line, line),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0
    except sqlite3.OperationalError:
        return None


def compute_interestingness(
    *,
    result: Mapping[str, Any],
    db_path: Path | str | None = None,
    target: str = "",
    sqlite_conn: sqlite3.Connection | None = None,
    **kwargs: Any,
) -> float:
    """
    Compute an "interestingness" score in [0.0, 1.0] for a single fuzzing input.

    The input is expected to be the top-level dictionary returned by
    parser.run_parser(...), i.e. something like:

        {
            "closed_result": {...},
            "open_result": {...}  # optional oracle result
        }

    AFL-style: when db_path/target are set, also uses seen_branches (read-only)
    and runs for new-edge and rare-bug scores. Main inserts into seen_branches.
    """
    top = _as_mapping(result)
    if top is None:
        return 0.0

    closed_raw = top.get("closed_result")
    closed = _as_mapping(closed_raw)
    if closed is None:
        return 0.0

    shadow_raw = top.get("shadow_result")
    shadow_res = _as_mapping(shadow_raw) if shadow_raw is not None else None

    open_raw = top.get("open_result")
    open_res = _as_mapping(open_raw) if open_raw is not None else None
    input_text = str(kwargs.get("input_text") or "")
    coverage_source = _select_coverage_source(
        closed=closed,
        shadow_res=shadow_res,
        open_res=open_res,
    )
    edges = _get_covered_edges(coverage_source)
    branch_edges = _get_covered_branch_edges(coverage_source)
    covered_lines = _get_covered_lines(coverage_source)
    coverage_key = _coverage_key_from_edges(edges)
    closed_status = _normalize_status(closed.get("status"))
    closed_bug = _as_mapping(closed.get("bug_signature"))

    conn = sqlite_conn
    close_conn = False
    if conn is None and db_path and Path(db_path).exists():
        path = Path(db_path) if isinstance(db_path, str) else db_path
        try:
            conn = open_results_db(path)
            close_conn = True
        except (sqlite3.Error, OSError):
            conn = None

    new_edges_signal = 0.0
    rare_edges_signal = 0.0
    new_branch_signal = 0.0
    new_statement_signal = 0.0
    new_diff_behavior_signal = 0.0
    new_error_site_signal = 0.0
    input_structure_novelty_signal = 0.0
    late_parse_depth_signal = max(
        late_parse_depth_from_result(result=top, input_text=input_text),
        partial_parse_success(result=top, input_text=input_text),
    )
    execution_stability_signal = execution_stability_bonus(
        closed_result=closed,
        open_result=open_res,
    )
    diff_behavior = build_differential_behavior(
        closed_result=closed,
        open_result=open_res,
    )
    diff_pattern_key = (
        str(diff_behavior.get("pattern_key") or "")
        if isinstance(diff_behavior, dict)
        else ""
    )
    structure_key = ""
    length_bucket = ""
    if input_text:
        structure_info = describe_input_structure(input_text)
        structure_key = str(structure_info.get("token_structure_key") or "")
        length_bucket = str(structure_info.get("length_bucket") or "")

    if conn is not None:
        try:
            new_edge_count, rare_edges_signal = _edge_novelty_metrics(
                conn,
                target=target,
                edges=edges,
            )
            new_edges_signal = 1.0 if new_edge_count > 0 else 0.0
            branch_stats = _new_edges_stats(conn, branch_edges)
            if branch_stats is not None:
                new_branch_signal = 1.0 if branch_stats[0] > 0 else 0.0
            line_stats = _new_lines_stats(conn, covered_lines)
            if line_stats is not None:
                new_statement_signal = 1.0 if line_stats[0] > 0 else 0.0
            if diff_pattern_key and _is_first_diff_behavior(
                conn,
                target=target,
                diff_pattern_key=diff_pattern_key,
            ):
                new_diff_behavior_signal = 1.0
            if input_text:
                input_structure_novelty_signal = _input_structure_novelty(
                    conn,
                    target=target,
                    coverage_key=coverage_key,
                    structure_key=structure_key,
                    length_bucket=length_bucket,
                )
            repeat_bug_site_count = _repeat_bug_site_count(
                conn,
                closed_status,
                closed_bug,
                target,
            )
            repeat_exception_site_count = _repeat_exception_site_count(
                conn,
                closed_status,
                closed_bug,
                target,
            )
            if (
                repeat_bug_site_count is not None
                and repeat_bug_site_count == 0
            ) or (
                repeat_exception_site_count is not None
                and repeat_exception_site_count == 0
            ):
                new_error_site_signal = 1.0
        except (sqlite3.Error, OSError):
            pass
        finally:
            if close_conn:
                conn.close()

    weighted_sum = (
        (5.0 * new_edges_signal)
        + (3.0 * rare_edges_signal)
        + (2.0 * new_branch_signal)
        + (1.5 * new_statement_signal)
        + (4.0 * new_diff_behavior_signal)
        + (3.0 * new_error_site_signal)
        + (2.0 * late_parse_depth_signal)
        + (2.0 * input_structure_novelty_signal)
    )
    if weighted_sum > 0.0:
        weighted_sum += 1.0 * execution_stability_signal

    score = min(1.0, weighted_sum / 8.0)
    return max(0.0, float(score))
