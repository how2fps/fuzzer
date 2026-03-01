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


def get_covered_edges_from_result(result: Mapping[str, Any]) -> set[tuple[str, int, int]]:
    """Extract (file, from_line, to_line) for all covered branches. Used by main to insert into seen_branches."""
    closed_raw = result.get("closed_result") if isinstance(result, Mapping) else None
    closed = _as_mapping(closed_raw)
    if closed is None:
        return set()
    return _get_covered_edges(closed)


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
    """Read-only: query seen_branches for which edges are new; return AFL-style score. No insert."""
    if not edges:
        return 0.0
    seen: set[tuple[str, int, int]] = set()
    try:
        cur = conn.execute("SELECT file, from_line, to_line FROM seen_branches")
        for row in cur:
            seen.add((str(row[0]), int(row[1]), int(row[2])))
    except sqlite3.OperationalError:
        pass
    new_edges = edges - seen
    new_ratio = len(new_edges) / float(len(edges))
    if new_ratio <= 0.0:
        return 0.0
    return 0.5 + 0.5 * min(new_ratio, 1.0)


def _rare_bug_score(
    conn: sqlite3.Connection,
    closed_status: str,
    closed_bug: Mapping[str, Any] | None,
    target: str,
) -> float:
    if closed_status not in {"bug", "crash", "timeout", "error"}:
        return 0.0
    if not closed_bug:
        return 0.0
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
            WHERE target = ? AND status IN ('bug', 'crash', 'timeout', 'error')
              AND COALESCE(exception, '') = COALESCE(?, '')
              AND COALESCE(file, '') = COALESCE(?, '')
              AND ((line IS NOT NULL AND line = ?) OR (line IS NULL AND ? IS NULL))
            """,
            (target, exc, file_, line, line),
        )
        row = cur.fetchone()
        count = int(row[0]) if row else 0
    except sqlite3.OperationalError:
        return 0.0
    return 1.0 / (1.0 + count)


def compute_interestingness(
    *,
    result: Mapping[str, Any],
    db_path: Path | str | None = None,
    target: str = "",
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

    covered_branches = closed.get("covered_branches")
    missing_branches = closed.get("missing_branches")

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
    score = max(s_status, s_diff, s_cov, 0.0)

    if db_path and Path(db_path).exists():
        path = Path(db_path) if isinstance(db_path, str) else db_path
        try:
            conn = sqlite3.connect(str(path))
            try:
                edges = _get_covered_edges(closed)
                s_new = _new_edges_score(conn, edges)
                s_rare = _rare_bug_score(conn, closed_status, closed_bug, target)
                score *= max(score, s_new, s_rare * 0.9, 0.0)
            finally:
                conn.close()
        except (sqlite3.Error, OSError):
            pass

    return max(0.0, min(1.0, float(score)))
