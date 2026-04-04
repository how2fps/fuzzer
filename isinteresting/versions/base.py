"""
Base interestingness: AFL-style scoring (status, differential, coverage,
new branches from seen_branches DB, rare-bug from runs DB).
Seen_branches insertion is done by main, not here.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

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


def _coverage_score(
    *,
    covered_branches: int | None,
    missing_branches: int | None,
) -> float:
    """
    Compute a simple coverage-based score from aggregate branch counts.
    """
    if covered_branches is None or missing_branches is None:
        return 0.0

    try:
        covered = int(covered_branches)
        missing = int(missing_branches)
    except (TypeError, ValueError):
        return 0.0

    if covered < 0 or missing < 0:
        return 0.0

    total = covered + missing
    if total <= 0:
        return 0.0

    ratio = covered / float(total)
    # Prefer inputs that execute more of the available branches.
    return max(0.0, min(ratio, 1.0))


def _has_coverage_counts(
    *,
    covered_branches: int | None,
    missing_branches: int | None,
) -> bool:
    return covered_branches is not None and missing_branches is not None


def get_covered_edges_from_result(result: Mapping[str, Any]) -> set[tuple[str, int, int]]:
    """Extract (file, from_line, to_line) for all covered branches. Used by main to insert into seen_branches."""
    closed_raw = result.get("closed_result") if isinstance(result, Mapping) else None
    closed = _as_mapping(closed_raw)
    if closed is None:
        return set()
    open_raw = result.get("open_result") if isinstance(result, Mapping) else None
    open_res = _as_mapping(open_raw) if open_raw is not None else None
    coverage_source = _select_coverage_source(closed=closed, open_res=open_res)
    return _get_covered_edges(coverage_source)


def get_coverage_source_kind_from_result(result: Mapping[str, Any]) -> str:
    """Return `closed`, `open`, or `none` for the coverage source selected for a result."""
    closed_raw = result.get("closed_result") if isinstance(result, Mapping) else None
    closed = _as_mapping(closed_raw)
    if closed is None:
        return "none"
    open_raw = result.get("open_result") if isinstance(result, Mapping) else None
    open_res = _as_mapping(open_raw) if open_raw is not None else None
    return _coverage_source_kind(closed=closed, open_res=open_res)


def _has_coverage_fields(value: Mapping[str, Any] | None) -> bool:
    if value is None:
        return False
    covered = value.get("covered_branches")
    missing = value.get("missing_branches")
    details = value.get("branch_details_by_file")
    return covered is not None and missing is not None and isinstance(details, Sequence)


def _coverage_source_kind(
    *,
    closed: Mapping[str, Any],
    open_res: Mapping[str, Any] | None,
) -> str:
    if _has_coverage_fields(closed):
        return "closed"
    if _has_coverage_fields(open_res):
        return "open"
    return "none"


def _select_coverage_source(
    *,
    closed: Mapping[str, Any],
    open_res: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    if _coverage_source_kind(closed=closed, open_res=open_res) == "closed":
        return closed
    if _coverage_source_kind(closed=closed, open_res=open_res) == "open":
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


def _new_edges_score(conn: sqlite3.Connection, edges: set[tuple[str, int, int]]) -> float:
    """Read-only: which result edges are absent from seen_branches; AFL-style score. No insert."""
    if not edges:
        return 0.0
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
        return 0.0
    new_ratio = new_count / float(len(edges))
    new_edge_presence = min(float(new_count), 1.0)
    return (0.5 * new_edge_presence) + (0.5 * min(new_ratio, 1.0))


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

    open_raw = top.get("open_result")
    open_res = _as_mapping(open_raw) if open_raw is not None else None

    closed_status = _normalize_status(closed.get("status"))
    open_status = _normalize_status(open_res.get("status")) if open_res else None

    closed_bug = _as_mapping(closed.get("bug_signature"))
    open_bug = _as_mapping(open_res.get("bug_signature")) if open_res else None

    coverage_source = _select_coverage_source(closed=closed, open_res=open_res)
    coverage_source_kind = _coverage_source_kind(closed=closed, open_res=open_res)
    covered_branches = coverage_source.get("covered_branches")
    missing_branches = coverage_source.get("missing_branches")

    s_status = _status_score(closed_status=closed_status)
    s_diff = _differential_score(
        closed_status=closed_status,
        open_status=open_status,
        closed_bug=closed_bug,
        open_bug=open_bug,
    )
    s_cov = _coverage_score(
        covered_branches=covered_branches,
        missing_branches=missing_branches,
    )
    has_coverage_counts = _has_coverage_counts(
        covered_branches=covered_branches,
        missing_branches=missing_branches,
    )
    repeat_bug_count: int | None = None
    repeat_bug_site_count: int | None = None
    repeat_exception_site_count: int | None = None
    s_new = 0.0
    s_rare = 0.0
    s_bug_site = 0.0
    s_exception_site = 0.0
    new_edge_weight = 2.0
    rare_bug_weight = 0.9
    bug_site_weight = 1.2
    exception_site_weight = 0.7
    metric_max = 2.0 + (1.0 if has_coverage_counts else 0.0)

    if sqlite_conn is not None:
        try:
            edges = _get_covered_edges(coverage_source)
            repeat_bug_count = _repeat_bug_count(
                sqlite_conn, closed_status, closed_bug, target
            )
            repeat_bug_site_count = _repeat_bug_site_count(
                sqlite_conn, closed_status, closed_bug, target
            )
            repeat_exception_site_count = _repeat_exception_site_count(
                sqlite_conn, closed_status, closed_bug, target
            )
            s_new = _new_edges_score(sqlite_conn, edges)
            s_rare = _rare_bug_score(repeat_bug_count)
            s_bug_site = _rare_bug_score(repeat_bug_site_count)
            s_exception_site = _rare_bug_score(repeat_exception_site_count)
            metric_max += (
                new_edge_weight
                + rare_bug_weight
                + bug_site_weight
                + exception_site_weight
            )
        except (sqlite3.Error, OSError):
            pass
    elif db_path and Path(db_path).exists():
        path = Path(db_path) if isinstance(db_path, str) else db_path
        try:
            conn = open_results_db(path)
            try:
                edges = _get_covered_edges(coverage_source)
                repeat_bug_count = _repeat_bug_count(
                    conn, closed_status, closed_bug, target
                )
                repeat_bug_site_count = _repeat_bug_site_count(
                    conn, closed_status, closed_bug, target
                )
                repeat_exception_site_count = _repeat_exception_site_count(
                    conn, closed_status, closed_bug, target
                )
                s_new = _new_edges_score(conn, edges)
                s_rare = _rare_bug_score(repeat_bug_count)
                s_bug_site = _rare_bug_score(repeat_bug_site_count)
                s_exception_site = _rare_bug_score(repeat_exception_site_count)
                metric_max += (
                    new_edge_weight
                    + rare_bug_weight
                    + bug_site_weight
                    + exception_site_weight
                )
            finally:
                conn.close()
        except (sqlite3.Error, OSError):
            pass

    # Keep rewarding novel coverage signals, but damp bug-related rewards when
    # the same bug signature has already been observed many times.
    repeated_bug_factor = _repeat_bug_factor(repeat_bug_count)
    support_signal = max(
        (s_status * repeated_bug_factor),
        s_diff,
        s_bug_site,
        s_exception_site,
        s_rare,
    )
    coverage_factor = 1.0
    if coverage_source_kind == "open":
        coverage_factor = 0.2 + (0.8 * support_signal)
    metric_sum = ((s_status + s_diff) * repeated_bug_factor) + (s_cov * coverage_factor)
    metric_sum += (s_new * new_edge_weight * coverage_factor)
    metric_sum += (s_rare * rare_bug_weight)
    metric_sum += (s_bug_site * bug_site_weight) + (
        s_exception_site * exception_site_weight
    )

    if metric_max <= 0.0:
        return 0.0
    weighted_score = metric_sum / metric_max
    if coverage_source_kind == "open":
        score = weighted_score
    else:
        # Blend through s_new so any positive new-coverage signal becomes a lower
        # bound on the final score instead of just another additive term.
        score = s_new + ((1.0 - s_new) * weighted_score)
    return max(0.0, min(1.0, float(score)))
