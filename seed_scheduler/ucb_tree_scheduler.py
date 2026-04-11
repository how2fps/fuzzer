from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.behavioral_signals import (
    build_differential_behavior,
    describe_input_structure,
    execution_stability_bonus,
    late_parse_depth_from_result,
    partial_parse_success,
    result_behavior_summary,
)
from isinteresting import (
    get_coverage_source_kind_from_result,
    get_covered_edges_from_result,
)
from seed_corpus import Seed

from core.fuzzer_logging import get_fuzzer_logger
from core.sqlite_conn import open_results_db

from .base import BaseSeedScheduler
from .types import ScheduledSeed

RECENT_NOVELTY_WINDOW = 16
RECENT_NOVELTY_REWARD = 0.35
SAME_COVERAGE_STREAK_PENALTY = 0.20
REPEATED_BUG_SITE_REWARD_ALPHA = 0.35
UCB_REWARD_EMA_ALPHA = 0.15
ISINTERESTING_SCORE_REWARD_WEIGHT = 0.75
DIVERSITY_FLOOR_PERIOD = 5
DIVERSITY_FLOOR_MIN_SELECTION_GAP = 2


def _short_hash(obj: Any) -> str:
    """Return a stable short hash for bucketing complex scheduler signal payloads."""
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8", errors="replace"
    )
    return hashlib.sha256(raw).hexdigest()[:16]


def _merge_line_ranges(values: list[int]) -> list[str]:
    """Merge sorted line numbers into compact inclusive ranges."""
    if not values:
        return []
    ordered = sorted(set(v for v in values if isinstance(v, int) and v > 0))
    if not ordered:
        return []
    ranges: list[str] = []
    start = ordered[0]
    end = ordered[0]
    for value in ordered[1:]:
        if value <= end + 1:
            end = value
            continue
        ranges.append(f"{start}-{end}" if start != end else str(start))
        start = value
        end = value
    ranges.append(f"{start}-{end}" if start != end else str(start))
    return ranges


def _summarize_branch_ranges(branch_details_by_file: Any) -> dict[str, dict[str, list[str]]]:
    """Summarize branch details as merged line ranges per file."""
    if not isinstance(branch_details_by_file, list):
        return {}
    summary: dict[str, dict[str, list[str]]] = {}
    for file_entry in branch_details_by_file:
        if not isinstance(file_entry, dict):
            continue
        file_name = file_entry.get("file")
        if not isinstance(file_name, str) or not file_name:
            continue
        covered_lines: list[int] = []
        missing_lines: list[int] = []
        for arc in file_entry.get("covered_branches", []):
            if isinstance(arc, dict):
                for key in ("from_line", "to_line"):
                    value = arc.get(key)
                    if isinstance(value, int) and value > 0:
                        covered_lines.append(value)
        for arc in file_entry.get("missing_branches", []):
            if isinstance(arc, dict):
                for key in ("from_line", "to_line"):
                    value = arc.get(key)
                    if isinstance(value, int) and value > 0:
                        missing_lines.append(value)
        summary[file_name] = {
            "covered": _merge_line_ranges(covered_lines),
            "missing": _merge_line_ranges(missing_lines),
        }
    return summary


def _summarize_trace_payload(
    *,
    raw_signals: dict[str, Any] | None,
    normalized_signals: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build a compact, human-readable summary of one UCB update payload."""
    closed = raw_signals.get("closed_result", {}) if isinstance(raw_signals, dict) else {}
    bug_signature = normalized_signals.get("bug_signature") if normalized_signals else None
    covered = closed.get("covered_branches")
    missing = closed.get("missing_branches")
    total = None
    if isinstance(covered, int) and isinstance(missing, int):
        total = covered + missing
    branch_ranges = _summarize_branch_ranges(closed.get("branch_details_by_file"))
    summary: dict[str, Any] = {
        "status": normalized_signals.get("status") if normalized_signals else None,
        "bug_type": bug_signature.get("type") if isinstance(bug_signature, dict) else None,
        "new_coverage": normalized_signals.get("new_coverage") if normalized_signals else None,
        "new_bug": normalized_signals.get("new_bug") if normalized_signals else None,
        "new_bug_site": normalized_signals.get("new_bug_site") if normalized_signals else None,
        "new_exception_site": normalized_signals.get("new_exception_site") if normalized_signals else None,
        "new_differential_behavior": (
            normalized_signals.get("new_differential_behavior")
            if normalized_signals
            else None
        ),
        "coverage_source": normalized_signals.get("coverage_source") if normalized_signals else None,
        "crash": normalized_signals.get("crash") if normalized_signals else None,
        "timeout": normalized_signals.get("timeout") if normalized_signals else None,
        "covered_branches": covered,
        "missing_branches": missing,
        "total_branches": total,
        "branch_ranges": branch_ranges,
    }
    return {key: value for key, value in summary.items() if value is not None}


def _compact_coverage_key(key: Any) -> str:
    """Render coverage bucket keys in a readable one-line form."""
    if isinstance(key, dict):
        if "family" in key or "bucket" in key:
            family = key.get("family", "?")
            bucket = key.get("bucket", "?")
            return f"family={family} bucket={bucket}"
        if "branch_details_by_file" in key:
            branch_ranges = _summarize_branch_ranges(key.get("branch_details_by_file"))
            parts: list[str] = []
            for file_name, ranges in list(branch_ranges.items())[:2]:
                short_file = file_name.rsplit("/", 1)[-1]
                covered = ", ".join(ranges.get("covered", [])[:2]) or "-"
                parts.append(f"{short_file}:{covered}")
            if len(branch_ranges) > 2:
                parts.append("...")
            return "ranges=" + " | ".join(parts)
        return json.dumps(key, sort_keys=True, default=str)[:120]
    return str(key)


def _coverage_key_from_edges(edges: set[tuple[str, int, int]]) -> str:
    """Build a deterministic compact coverage key from covered edges."""
    if not edges:
        return "NO_COVERAGE"
    # Sort for deterministic hashing across process boundaries.
    ordered = sorted(edges)
    return "COV:" + _short_hash(ordered)


def _edge_novelty_metrics(
    *,
    db_path: Path | str,
    result: dict[str, Any],
    target: str,
    sqlite_conn: sqlite3.Connection | None = None,
) -> tuple[int, float]:
    edges = get_covered_edges_from_result(result)
    if not edges:
        return (0, 0.0)

    def _compute(conn: sqlite3.Connection) -> tuple[int, float]:
        new_edge_count = 0
        rare_total = 0.0
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
                new_edge_count += 1
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
        return (new_edge_count, min(1.0, rare_total / float(len(edges))))

    if sqlite_conn is not None:
        try:
            return _compute(sqlite_conn)
        except sqlite3.OperationalError:
            return (0, 0.0)

    path = Path(db_path) if isinstance(db_path, str) else db_path
    if not path.exists():
        return (0, 0.0)
    try:
        conn = open_results_db(path)
        try:
            return _compute(conn)
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return (0, 0.0)


def _is_first_diff_behavior(
    *,
    db_path: Path | str,
    target: str,
    diff_pattern_key: str,
    sqlite_conn: sqlite3.Connection | None = None,
) -> bool:
    if not diff_pattern_key:
        return False

    def _check(conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            """
            SELECT 1
            FROM runs
            WHERE target = ? AND COALESCE(diff_pattern_key, '') = COALESCE(?, '')
            LIMIT 1
            """,
            (target, diff_pattern_key),
        ).fetchone()
        return row is None

    if sqlite_conn is not None:
        try:
            return _check(sqlite_conn)
        except sqlite3.OperationalError:
            return False

    path = Path(db_path) if isinstance(db_path, str) else db_path
    if not path.exists():
        return False
    try:
        conn = open_results_db(path)
        try:
            return _check(conn)
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return False


def _input_structure_novelty(
    *,
    db_path: Path | str,
    target: str,
    coverage_key: str,
    structure_key: str,
    length_bucket: str,
    sqlite_conn: sqlite3.Connection | None = None,
) -> float:
    if not structure_key:
        return 0.0

    def _query_exists(
        conn: sqlite3.Connection,
        *,
        column: str,
        value: str,
    ) -> bool:
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

    def _compute(conn: sqlite3.Connection) -> float:
        novelty = 0.0
        if not _query_exists(conn, column="structure_key", value=structure_key):
            novelty += 0.7
        if length_bucket and not _query_exists(
            conn,
            column="length_bucket",
            value=length_bucket,
        ):
            novelty += 0.3
        return min(1.0, novelty)

    if sqlite_conn is not None:
        try:
            return _compute(sqlite_conn)
        except sqlite3.OperationalError:
            return 0.0

    path = Path(db_path) if isinstance(db_path, str) else db_path
    if not path.exists():
        return 0.0
    try:
        conn = open_results_db(path)
        try:
            return _compute(conn)
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return 0.0


def _track_item_recency(
    item: ScheduledSeed,
    signals: dict[str, Any] | None,
) -> tuple[float, int]:
    history = item.metadata.get("_recent_novelty_history")
    if not isinstance(history, list):
        history = []

    last_cov_key = item.metadata.get("_last_coverage_key")
    same_coverage_streak = max(0, int(item.metadata.get("_same_coverage_streak", 0)))

    coverage_key = None
    if isinstance(signals, dict):
        raw_coverage_key = signals.get("coverage_key")
        if raw_coverage_key not in (None, "", [], {}):
            coverage_key = str(raw_coverage_key)
    novelty = False
    if isinstance(signals, dict):
        novelty = any(
            bool(signals.get(key))
            for key in (
                "new_coverage",
                "new_bug",
                "new_bug_site",
                "new_exception_site",
                "new_differential_behavior",
            )
        )

    if novelty:
        same_coverage_streak = 0
    elif coverage_key is None:
        same_coverage_streak = 0
    elif coverage_key == last_cov_key:
        same_coverage_streak += 1
    else:
        same_coverage_streak = 1

    if coverage_key is not None:
        item.metadata["_last_coverage_key"] = coverage_key
    history.append(1 if novelty else 0)
    if len(history) > RECENT_NOVELTY_WINDOW:
        history = history[-RECENT_NOVELTY_WINDOW:]
    item.metadata["_recent_novelty_history"] = history
    item.metadata["_same_coverage_streak"] = same_coverage_streak

    recent_novelty_rate = (
        sum(history) / float(len(history))
        if history
        else 0.0
    )
    item.metadata["_recent_novelty_rate"] = recent_novelty_rate
    return recent_novelty_rate, same_coverage_streak


def _bug_site_key(signals: dict[str, Any] | None) -> str | None:
    if not isinstance(signals, dict):
        return None
    bug_signature = signals.get("bug_signature")
    if not isinstance(bug_signature, dict):
        return None
    file_ = str(bug_signature.get("file") or "").strip()
    line_raw = bug_signature.get("line")
    try:
        line = int(line_raw)
    except (TypeError, ValueError):
        line = None
    if line is None:
        return None
    return f"{file_ or '?'}:{line}"


def _track_bug_site_repetition(
    bug_site_hit_counts: dict[str, int],
    signals: dict[str, Any] | None,
) -> int:
    bug_site_key = _bug_site_key(signals)
    if bug_site_key is None:
        return 0

    previous_hits = int(bug_site_hit_counts.get(bug_site_key, 0))
    bug_site_hit_counts[bug_site_key] = previous_hits + 1
    return previous_hits


def _flat_compact_signals(
    *,
    result: dict[str, Any],
    status: str,
    score: float,
    new_coverage: bool,
    new_bug: bool,
    new_bug_site: bool,
    new_exception_site: bool,
    new_differential_behavior: bool,
    coverage_source: str,
    new_edge_count: int,
    rare_edge_score: float,
    new_error_site: bool,
    parse_category: str,
    output_signature: str,
    output_class: str,
    error_code: str,
    diff_behavior_key: str,
    diff_pattern_key: str,
    mismatch_type_key: str,
    structure_key: str,
    length_bucket: str,
    input_structure_novelty: float,
    late_parse_depth: float,
    partial_parse_success: float,
    execution_stability_bonus: float,
) -> dict[str, Any]:
    """Return a compact flat signal payload to reduce queue memory pressure."""
    closed_raw = result.get("closed_result", {})
    open_raw = result.get("open_result", {})
    closed = closed_raw if isinstance(closed_raw, dict) else {}
    open_ = open_raw if isinstance(open_raw, dict) else {}
    bug_signature = closed.get("bug_signature") or open_.get("bug_signature")
    edges = get_covered_edges_from_result(result)
    representative_payload = {
        "diff_pattern_key": diff_pattern_key,
        "mismatch_type_key": mismatch_type_key,
        "bug_signature": bug_signature,
        "parse_category": parse_category,
        "output_class": output_class,
        "error_code": error_code,
        "structure_key": structure_key,
        "length_bucket": length_bucket,
    }
    representative_meaningful = {
        key: value
        for key, value in representative_payload.items()
        if value not in (None, "", [], {})
    }
    representative_key = (
        "REP:" + _short_hash(representative_meaningful)
        if representative_meaningful
        else ""
    )

    out: dict[str, Any] = {
        "status": status,
        "isinteresting": score,
        "new_coverage": new_coverage,
        "new_bug": new_bug,
        "new_bug_site": new_bug_site,
        "new_exception_site": new_exception_site,
        "new_error_site": new_error_site,
        "new_differential_behavior": new_differential_behavior,
        "coverage_source": coverage_source,
        "crash": status == "crash",
        "timeout": status == "timeout",
        "coverage_key": _coverage_key_from_edges(edges),
        "new_edge_count": new_edge_count,
        "rare_edge_score": rare_edge_score,
        "parse_category": parse_category,
        "output_signature": output_signature,
        "output_class": output_class,
        "error_code": error_code,
        "diff_behavior_key": diff_behavior_key,
        "diff_pattern_key": diff_pattern_key,
        "mismatch_type_key": mismatch_type_key,
        "structure_key": structure_key,
        "length_bucket": length_bucket,
        "input_structure_novelty": input_structure_novelty,
        "late_parse_depth": late_parse_depth,
        "partial_parse_success": partial_parse_success,
        "execution_stability_bonus": execution_stability_bonus,
    }
    if representative_key:
        out["representative_key"] = representative_key
    if bug_signature:
        out["bug_signature"] = bug_signature
        if isinstance(bug_signature, dict):
            meaningful = {
                key: value
                for key, value in bug_signature.items()
                if value not in (None, "", [], {})
            }
            if meaningful:
                out["bug_key"] = "BUG:" + _short_hash(meaningful)
    if closed.get("stdout_signature") not in (None, ""):
        out["stdout_signature"] = closed.get("stdout_signature")
    if closed.get("stderr_signature") not in (None, ""):
        out["stderr_signature"] = closed.get("stderr_signature")
    if closed.get("semantic_output_signature") not in (None, ""):
        out["semantic_output_signature"] = closed.get("semantic_output_signature")
    return out


def _closed_status(result: dict[str, Any]) -> str:
    """Return normalized closed_result status."""
    closed = result.get("closed_result", {})
    status = closed.get("status")
    return str(status).strip().lower() if isinstance(status, str) else ""


def _has_new_coverage(
    db_path: Path | str,
    result: dict[str, Any],
    *,
    sqlite_conn: sqlite3.Connection | None = None,
) -> bool:
    """Return True if the current result covers any edge not yet in seen_branches."""
    edges = get_covered_edges_from_result(result)
    if not edges:
        return False

    def _any_new(conn: sqlite3.Connection) -> bool:
        try:
            for f, fl, tl in edges:
                row = conn.execute(
                    "SELECT 1 FROM seen_branches WHERE file = ? AND from_line = ? AND to_line = ? LIMIT 1",
                    (f, fl, tl),
                ).fetchone()
                if row is None:
                    return True
            return False
        except sqlite3.OperationalError:
            return False

    if sqlite_conn is not None:
        return _any_new(sqlite_conn)

    path = Path(db_path) if isinstance(db_path, str) else db_path
    if not path.exists():
        return False
    try:
        conn = open_results_db(path)
        try:
            return _any_new(conn)
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return False


def _has_new_bug(
    db_path: Path | str,
    result: dict[str, Any],
    target: str,
    *,
    sqlite_conn: sqlite3.Connection | None = None,
) -> bool:
    """Return True if the current bug/crash signature has not appeared before for this target."""
    status = _closed_status(result)
    if status not in {"bug", "crash", "timeout", "error"}:
        return False
    closed = result.get("closed_result", {})
    bug_signature = closed.get("bug_signature") or {}
    if not isinstance(bug_signature, dict):
        return False
    exc = bug_signature.get("exception") or ""
    file_ = bug_signature.get("file") or ""
    line_raw = bug_signature.get("line")
    line = None
    if line_raw is not None:
        try:
            line = int(line_raw)
        except (TypeError, ValueError):
            line = None

    def _is_first_occurrence(conn: sqlite3.Connection) -> bool:
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
        return int(row[0]) == 0 if row else False

    if sqlite_conn is not None:
        try:
            return _is_first_occurrence(sqlite_conn)
        except sqlite3.OperationalError:
            return False

    path = Path(db_path) if isinstance(db_path, str) else db_path
    if not path.exists():
        return False
    try:
        conn = open_results_db(path)
        try:
            return _is_first_occurrence(conn)
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return False


def _has_new_bug_site(
    db_path: Path | str,
    result: dict[str, Any],
    target: str,
    *,
    sqlite_conn: sqlite3.Connection | None = None,
) -> bool:
    status = _closed_status(result)
    if status not in {"bug", "crash", "timeout", "error"}:
        return False
    closed = result.get("closed_result", {})
    bug_signature = closed.get("bug_signature") or {}
    if not isinstance(bug_signature, dict):
        return False
    file_ = bug_signature.get("file") or ""
    line_raw = bug_signature.get("line")
    line = None
    if line_raw is not None:
        try:
            line = int(line_raw)
        except (TypeError, ValueError):
            line = None
    if not file_ and line is None:
        return False

    def _is_first_occurrence(conn: sqlite3.Connection) -> bool:
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
        return int(row[0]) == 0 if row else False

    if sqlite_conn is not None:
        try:
            return _is_first_occurrence(sqlite_conn)
        except sqlite3.OperationalError:
            return False

    path = Path(db_path) if isinstance(db_path, str) else db_path
    if not path.exists():
        return False
    try:
        conn = open_results_db(path)
        try:
            return _is_first_occurrence(conn)
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return False


def _has_new_exception_site(
    db_path: Path | str,
    result: dict[str, Any],
    target: str,
    *,
    sqlite_conn: sqlite3.Connection | None = None,
) -> bool:
    status = _closed_status(result)
    if status not in {"bug", "crash", "timeout", "error"}:
        return False
    closed = result.get("closed_result", {})
    bug_signature = closed.get("bug_signature") or {}
    if not isinstance(bug_signature, dict):
        return False
    exc = bug_signature.get("exception") or ""
    file_ = bug_signature.get("file") or ""
    line_raw = bug_signature.get("line")
    line = None
    if line_raw is not None:
        try:
            line = int(line_raw)
        except (TypeError, ValueError):
            line = None
    if not exc:
        return False

    def _is_first_occurrence(conn: sqlite3.Connection) -> bool:
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
        return int(row[0]) == 0 if row else False

    if sqlite_conn is not None:
        try:
            return _is_first_occurrence(sqlite_conn)
        except sqlite3.OperationalError:
            return False

    path = Path(db_path) if isinstance(db_path, str) else db_path
    if not path.exists():
        return False
    try:
        conn = open_results_db(path)
        try:
            return _is_first_occurrence(conn)
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return False


def build_ucb_update_signals(
    *,
    result: dict[str, Any],
    db_path: Path | str,
    target: str,
    bucket: str,
    iteration: int,
    seed_id: str,
    score: float,
    input_text: str = "",
    sqlite_conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Build the per-mutation feedback payload consumed by the UCB scheduler."""
    status = _closed_status(result)
    coverage_source = get_coverage_source_kind_from_result(result)
    closed_raw = result.get("closed_result", {})
    open_raw = result.get("open_result", {})
    closed = closed_raw if isinstance(closed_raw, dict) else {}
    open_ = open_raw if isinstance(open_raw, dict) else {}
    raw_new_coverage = _has_new_coverage(db_path, result, sqlite_conn=sqlite_conn)
    new_edge_count, rare_edge_score = _edge_novelty_metrics(
        db_path=db_path,
        result=result,
        target=target,
        sqlite_conn=sqlite_conn,
    )
    new_bug = _has_new_bug(db_path, result, target, sqlite_conn=sqlite_conn)
    new_bug_site = _has_new_bug_site(
        db_path,
        result,
        target,
        sqlite_conn=sqlite_conn,
    )
    new_exception_site = _has_new_exception_site(
        db_path,
        result,
        target,
        sqlite_conn=sqlite_conn,
    )
    diff_behavior = build_differential_behavior(
        closed_result=closed,
        open_result=open_,
    )
    diff_behavior_key = (
        str(diff_behavior.get("behavior_key") or "")
        if isinstance(diff_behavior, dict)
        else ""
    )
    diff_pattern_key = (
        str(diff_behavior.get("pattern_key") or "")
        if isinstance(diff_behavior, dict)
        else ""
    )
    mismatch_type_key = (
        str(diff_behavior.get("mismatch_type_key") or "")
        if isinstance(diff_behavior, dict)
        else ""
    )
    new_differential_behavior = (
        _is_first_diff_behavior(
            db_path=db_path,
            target=target,
            diff_pattern_key=diff_pattern_key,
            sqlite_conn=sqlite_conn,
        )
        if diff_pattern_key
        else False
    )
    new_coverage = raw_new_coverage and (
        coverage_source != "open"
        or new_bug
        or new_bug_site
        or new_exception_site
        or new_differential_behavior
    )
    behavior_summary = result_behavior_summary(closed)
    structure_key = ""
    length_bucket = ""
    if input_text:
        structure_info = describe_input_structure(input_text)
        structure_key = str(structure_info.get("token_structure_key") or "")
        length_bucket = str(structure_info.get("length_bucket") or "")
    coverage_key = _coverage_key_from_edges(get_covered_edges_from_result(result))
    input_structure_novelty = (
        _input_structure_novelty(
            db_path=db_path,
            target=target,
            coverage_key=coverage_key,
            structure_key=structure_key,
            length_bucket=length_bucket,
            sqlite_conn=sqlite_conn,
        )
        if input_text
        else 0.0
    )
    late_parse_depth = late_parse_depth_from_result(
        result=result,
        input_text=input_text,
    )
    partial_success = partial_parse_success(
        result=result,
        input_text=input_text,
    )
    stability_bonus = execution_stability_bonus(
        closed_result=closed,
        open_result=open_,
    )
    new_error_site = new_bug_site or new_exception_site
    compact = _flat_compact_signals(
        result=result,
        status=status,
        score=score,
        new_coverage=new_coverage,
        new_bug=new_bug,
        new_bug_site=new_bug_site,
        new_exception_site=new_exception_site,
        new_differential_behavior=new_differential_behavior,
        coverage_source=coverage_source,
        new_edge_count=new_edge_count,
        rare_edge_score=rare_edge_score,
        new_error_site=new_error_site,
        parse_category=str(behavior_summary.get("parse_category") or ""),
        output_signature=str(behavior_summary.get("output_signature") or ""),
        output_class=str(behavior_summary.get("output_class") or ""),
        error_code=str(behavior_summary.get("error_code") or ""),
        diff_behavior_key=diff_behavior_key,
        diff_pattern_key=diff_pattern_key,
        mismatch_type_key=mismatch_type_key,
        structure_key=structure_key,
        length_bucket=length_bucket,
        input_structure_novelty=input_structure_novelty,
        late_parse_depth=max(late_parse_depth, partial_success),
        partial_parse_success=partial_success,
        execution_stability_bonus=stability_bonus,
    )
    compact["iteration"] = iteration
    compact["seed_id"] = seed_id
    compact["bucket"] = bucket
    return compact


@dataclass
class _TreeNode:
    """Tree node used to group scheduled items by coverage and bug buckets."""

    kind: str  # root | coverage | bug
    key: str
    parent: _TreeNode | None = None
    children: dict[str, _TreeNode] = field(default_factory=dict)
    seeds: list[ScheduledSeed] = field(default_factory=list)  # for bug nodes only
    n_selected: int = 0
    q_avg_reward: float = 0.0
    rr_index: int = 0

    def update_stats(self, reward: float) -> None:
        """Update running UCB reward statistics using a discounted EMA."""
        self.n_selected += 1
        if self.n_selected == 1:
            self.q_avg_reward = reward
            return
        self.q_avg_reward = (
            ((1.0 - UCB_REWARD_EMA_ALPHA) * self.q_avg_reward)
            + (UCB_REWARD_EMA_ALPHA * reward)
        )


class UCBTreeScheduler(BaseSeedScheduler):
    """
    root -> coverage bucket -> bug/output bucket -> seeds

    UCB1 is used at each internal node to select the next child.
    Reward is computed from `signals` inside `update()` (Option A).
    """

    def __init__(
        self,
        *,
        ucb_c: float = 1.0,
        max_seeds_per_leaf: int = 16,
        diversity_floor_period: int = DIVERSITY_FLOOR_PERIOD,
        diversity_floor_min_selection_gap: int = DIVERSITY_FLOOR_MIN_SELECTION_GAP,
    ) -> None:
        """Initialize tree structure and UCB exploration parameters."""
        self._ucb_c = float(ucb_c)
        self._max_seeds_per_leaf = int(max_seeds_per_leaf)
        self._diversity_floor_period = max(0, int(diversity_floor_period))
        self._diversity_floor_min_selection_gap = max(
            0,
            int(diversity_floor_min_selection_gap),
        )
        self._root = _TreeNode(kind="root", key="root")
        self._items: dict[str, ScheduledSeed] = {}
        self._bug_site_hit_counts: dict[str, int] = {}
        self._seq = 0
        self._leases_issued = 0

    def add(self, seed: Seed, *, metadata: dict[str, Any] | None = None) -> ScheduledSeed:
        """Insert a seed into the coverage/bug leaf selected from its metadata signals."""
        metadata = dict(metadata or {})
        metadata.setdefault("_recent_novelty_history", [])
        metadata.setdefault("_same_coverage_streak", 0)
        metadata.setdefault("_recent_novelty_rate", 0.0)
        signals = self._normalize_signals(metadata.get("signals"))
        cov_key = self._coverage_bucket_key(signals)
        bug_key = self._bug_bucket_key(signals)
        leaf = self._ensure_leaf(cov_key, bug_key)

        self._seq += 1
        item = ScheduledSeed(
            item_id=f"u{self._seq:06d}",
            seed=seed,
            priority=0.0,
            metadata=metadata,
        )
        item.metadata["_ucb_insert_seq"] = self._seq
        item.metadata["_ucb_home"] = (cov_key, bug_key)
        self._items[item.item_id] = item
        self._insert_into_leaf(leaf, item)
        return item

    def next(self) -> ScheduledSeed:
        """Traverse the tree with UCB1 and return one scheduled item from the chosen leaf."""
        if self.empty():
            raise IndexError("scheduler is empty")

        diversity_pick = None
        if self._should_take_diversity_pick():
            diversity_pick = self._take_diversity_pick()

        if diversity_pick is not None:
            item, path = diversity_pick
        else:
            path = [self._root]
            node = self._root
            while node.kind != "bug":
                child = self._select_ucb_child(node)
                if child is None:
                    raise IndexError("no selectable child")
                path.append(child)
                node = child

            if not node.seeds:
                raise IndexError("selected empty leaf")

            if node.rr_index >= len(node.seeds):
                node.rr_index = 0
            item = node.seeds.pop(node.rr_index)

        item.times_selected += 1
        self._leases_issued += 1
        item.metadata["_ucb_last_path"] = path
        item.metadata["_ucb_last_leaf"] = (path[-2].key, path[-1].key)
        return item

    def begin_batch(self, item: ScheduledSeed, *, batch_size: int) -> None:
        """Track how many batch results must arrive before the item becomes ready again."""
        if item.item_id not in self._items:
            raise KeyError(f"unknown item_id {item.item_id!r}")
        stored = self._items[item.item_id]
        stored.metadata["_ucb_pending_batch_results"] = max(1, int(batch_size))

    def update(
        self,
        item: ScheduledSeed,
        *,
        isinteresting_score: float,
        signals: dict[str, Any] | None = None,
    ) -> ScheduledSeed:
        """Update rewards for the leased path and reinsert the item into its selected leaf."""
        if item.item_id not in self._items:
            raise KeyError(f"unknown item_id {item.item_id!r}")

        stored = self._items[item.item_id]
        stored.last_isinteresting_score = float(isinteresting_score)
        stored.total_isinteresting_score += float(isinteresting_score)
        stored.updates += 1
        normalized_signals = self._normalize_signals(signals)
        if normalized_signals:
            stored.metadata["last_signals"] = normalized_signals

        recent_novelty_rate, same_coverage_streak = _track_item_recency(
            stored,
            normalized_signals,
        )
        repeated_bug_site_hits = _track_bug_site_repetition(
            self._bug_site_hit_counts,
            normalized_signals,
        )
        reward = self._reward_from_signals(
            normalized_signals,
            isinteresting_score=isinteresting_score,
            recent_novelty_rate=recent_novelty_rate,
            same_coverage_streak=same_coverage_streak,
            repeated_bug_site_hits=repeated_bug_site_hits,
        )
        path = stored.metadata.get("_ucb_last_path")
        if not path:
            raise ValueError("update() called before next() for this item")
        for node in path:
            node.update_stats(reward)

        if normalized_signals:
            cov_key = self._coverage_bucket_key(normalized_signals)
            bug_key = self._bug_bucket_key(normalized_signals)
        else:
            cov_key, bug_key = stored.metadata.get("_ucb_last_leaf") or stored.metadata.get(
                "_ucb_home", ("NO_COVERAGE", "NO_BUG")
            )
        trace = stored.metadata.get("_ucb_trace")
        if trace:
            trace_summary = _summarize_trace_payload(
                raw_signals=signals,
                normalized_signals=normalized_signals,
            )
            get_fuzzer_logger().info(
                "[ucb.update] item=%s seed=%s score=%.3f reward=%.3f leaf=(%s, %s) summary=%r",
                stored.item_id,
                stored.seed.seed_id,
                isinteresting_score,
                reward,
                cov_key,
                bug_key,
                trace_summary,
            )
        remaining = int(stored.metadata.get("_ucb_pending_batch_results", 1))
        remaining = max(remaining - 1, 0)
        stored.metadata["_ucb_pending_batch_results"] = remaining
        if remaining == 0:
            stored.metadata["_ucb_home"] = (cov_key, bug_key)
            stored.metadata["_ucb_last_leaf"] = (cov_key, bug_key)
            stored.metadata.pop("_ucb_last_path", None)
            leaf = self._ensure_leaf(cov_key, bug_key)
            self._insert_into_leaf(leaf, stored)
        else:
            # Keep the leased traversal path alive until the full batch has reported
            # back; one `next()` lease can produce multiple feedback updates.
            stored.metadata["_ucb_last_leaf"] = (cov_key, bug_key)
        return stored

    def empty(self) -> bool:
        """Return True when no leaf currently holds any schedulable items."""
        return self._available_count(self._root) == 0

    def __len__(self) -> int:
        """Return the number of ready items across all leaves."""
        return self._available_count(self._root)

    def stats(self) -> dict[str, Any]:
        """Return aggregate tree size and parameter metrics."""
        coverage_buckets = len(self._root.children)
        bug_buckets = sum(len(c.children) for c in self._root.children.values())
        return {
            "kind": "ucb_tree",
            "ready": len(self),
            "total_items": len(self._items),
            "coverage_buckets": coverage_buckets,
            "bug_buckets": bug_buckets,
            "ucb_c": self._ucb_c,
            "max_seeds_per_leaf": self._max_seeds_per_leaf,
            "diversity_floor_period": self._diversity_floor_period,
            "diversity_floor_min_selection_gap": self._diversity_floor_min_selection_gap,
        }

    def ready_items(self) -> list[ScheduledSeed]:
        """Return ready items across all leaves without mutating traversal state."""
        items: list[ScheduledSeed] = []
        for cov_node in self._root.children.values():
            for bug_node in cov_node.children.values():
                items.extend(bug_node.seeds)
        return items

    def debug_dump(self, limit: int = 20) -> dict[str, Any]:
        """Return a leaf-oriented snapshot ordered by current average reward."""
        leaves: list[dict[str, Any]] = []
        for cov_key, cov_node in self._root.children.items():
            for bug_key, bug_node in cov_node.children.items():
                if not bug_node.seeds:
                    continue
                leaves.append(
                    {
                        "coverage_key": cov_key,
                        "bug_key": bug_key,
                        "leaf_n_selected": bug_node.n_selected,
                        "leaf_q_avg_reward": round(bug_node.q_avg_reward, 4),
                        "seed_count": len(bug_node.seeds),
                        "seed_ids": [s.seed.seed_id for s in bug_node.seeds[:5]],
                    }
                )
        # Surface the leaves with highest current Q first for a useful snapshot.
        leaves.sort(
            key=lambda x: (-x["leaf_q_avg_reward"], -x["leaf_n_selected"], x["coverage_key"], x["bug_key"])
        )
        return {
            "stats": self.stats(),
            "leaves": leaves[: max(limit, 0)],
            "truncated": len(leaves) > min(max(limit, 0), len(leaves)),
        }

    def supports_feedback_updates(self) -> bool:
        return True

    def render_tree(self, limit: int = 20) -> str:
        """Render a readable tree snapshot for logging/debugging."""
        lines = [
            "ucb_tree",
            (
                f"root ready={len(self)} total_items={len(self._items)} "
                f"coverage_buckets={len(self._root.children)} ucb_c={self._ucb_c}"
            ),
        ]
        emitted = 0
        coverage_nodes = sorted(
            self._root.children.values(),
            key=lambda node: (-node.q_avg_reward, -node.n_selected, node.key),
        )
        for cov_node in coverage_nodes:
            if emitted >= limit:
                break
            lines.append(
                f"|- cov {_compact_coverage_key(cov_node.key)} N={cov_node.n_selected} Q={cov_node.q_avg_reward:.3f}"
            )
            bug_nodes = sorted(
                cov_node.children.values(),
                key=lambda node: (-node.q_avg_reward, -node.n_selected, node.key),
            )
            for bug_node in bug_nodes:
                if emitted >= limit:
                    break
                seed_ids = ", ".join(seed.seed.seed_id for seed in bug_node.seeds[:4])
                if len(bug_node.seeds) > 4:
                    seed_ids += ", ..."
                lines.append(
                    (
                        f"|  |- bug {bug_node.key} N={bug_node.n_selected} "
                        f"Q={bug_node.q_avg_reward:.3f} seeds={len(bug_node.seeds)} "
                        f"rr={bug_node.rr_index}"
                    )
                )
                if seed_ids:
                    lines.append(f"|  |  `- {seed_ids}")
                emitted += 1
        if emitted >= limit:
            lines.append("`- ...")
        return "\n".join(lines)

    def _ensure_leaf(self, cov_key: str, bug_key: str) -> _TreeNode:
        """Create or return the leaf node for a coverage/bug bucket pair."""
        cov = self._root.children.get(cov_key)
        if cov is None:
            cov = _TreeNode(kind="coverage", key=cov_key, parent=self._root)
            self._root.children[cov_key] = cov
        bug = cov.children.get(bug_key)
        if bug is None:
            bug = _TreeNode(kind="bug", key=bug_key, parent=cov)
            cov.children[bug_key] = bug
        return bug

    def _insert_into_leaf(self, leaf: _TreeNode, item: ScheduledSeed) -> None:
        """Insert an item into a leaf and evict overflow items beyond the leaf limit."""
        leaf.seeds.append(item)
        if len(leaf.seeds) > self._max_seeds_per_leaf:
            leaf.seeds.sort(key=self._leaf_retention_key, reverse=True)
            evicted = leaf.seeds[self._max_seeds_per_leaf:]
            leaf.seeds = leaf.seeds[: self._max_seeds_per_leaf]
            if leaf.rr_index > len(leaf.seeds):
                leaf.rr_index = len(leaf.seeds)
            for old in evicted:
                # If the just-added item gets evicted, also drop it from item registry.
                self._items.pop(old.item_id, None)

    def _should_take_diversity_pick(self) -> bool:
        """Periodically force a least-selected ready seed back into circulation."""
        if self._diversity_floor_period <= 0:
            return False
        if (self._leases_issued + 1) % self._diversity_floor_period != 0:
            return False
        ready_items = self.ready_items()
        if len(ready_items) < 2:
            return False
        times_selected = [item.times_selected for item in ready_items]
        if not times_selected:
            return False
        return (
            max(times_selected) - min(times_selected)
            >= self._diversity_floor_min_selection_gap
        )

    def _take_diversity_pick(self) -> tuple[ScheduledSeed, list[_TreeNode]] | None:
        """Pick the least-selected ready seed across leaves and remove it from readiness."""
        best_choice: tuple[
            tuple[int, int, int, float, int, str],
            _TreeNode,
            int,
            ScheduledSeed,
            list[_TreeNode],
        ] | None = None
        for cov_node in self._root.children.values():
            for bug_node in cov_node.children.values():
                if not bug_node.seeds:
                    continue
                path = [self._root, cov_node, bug_node]
                for index, item in enumerate(bug_node.seeds):
                    key = self._diversity_candidate_key(item, bug_node)
                    if best_choice is None or key < best_choice[0]:
                        best_choice = (key, bug_node, index, item, path)
        if best_choice is None:
            return None

        _key, leaf, index, item, path = best_choice
        leaf.seeds.pop(index)
        if leaf.rr_index > index:
            leaf.rr_index -= 1
        if leaf.rr_index >= len(leaf.seeds):
            leaf.rr_index = 0
        return item, path

    def _diversity_candidate_key(
        self,
        item: ScheduledSeed,
        leaf: _TreeNode,
    ) -> tuple[int, int, int, float, int, str]:
        """Rank ready items for diversity picks while keeping deterministic tie-breaks."""
        unseen_rank = 0 if item.updates == 0 else 1
        insert_seq = int(item.metadata.get("_ucb_insert_seq", 0))
        return (
            item.times_selected,
            unseen_rank,
            leaf.n_selected,
            -item.avg_isinteresting_score,
            insert_seq,
            item.seed.seed_id,
        )

    def _leaf_retention_key(self, item: ScheduledSeed) -> tuple[float, float, float, float]:
        """
        Rank items to keep when a leaf overflows.

        Prefer unseen seeds first so new additions get evaluated at least once,
        then prefer historically higher-value seeds, then less-selected seeds,
        and finally newer arrivals as a deterministic tiebreaker.
        """
        is_unseen = 1.0 if item.updates == 0 else 0.0
        avg_score = item.avg_isinteresting_score
        less_selected = -float(item.times_selected)
        insert_seq = float(item.metadata.get("_ucb_insert_seq", 0))
        return (is_unseen, avg_score, less_selected, insert_seq)

    def _select_ucb_child(self, parent: _TreeNode) -> _TreeNode | None:
        """Select the next child node to traverse using the UCB1 score."""
        candidates = [c for c in parent.children.values() if self._available_count(c) > 0]
        if not candidates:
            return None

        best = None
        best_score = -math.inf
        for child in candidates:
            score = self._ucb_score(parent, child)
            if score > best_score:
                best_score = score
                best = child
        return best

    def _ucb_score(self, parent: _TreeNode, child: _TreeNode) -> float:
        """Compute the UCB1 score for one child relative to its parent."""
        if child.n_selected == 0:
            return math.inf
        parent_n = max(parent.n_selected, 1)
        return child.q_avg_reward + self._ucb_c * math.sqrt(
            math.log(parent_n) / child.n_selected
        )

    def _available_count(self, node: _TreeNode) -> int:
        """Count schedulable items reachable from a node."""
        if node.kind == "bug":
            return len(node.seeds)
        return sum(self._available_count(child) for child in node.children.values())

    def _reward_from_signals(
        self,
        signals: dict[str, Any] | None,
        *,
        isinteresting_score: float | None = None,
        recent_novelty_rate: float = 0.0,
        same_coverage_streak: int = 0,
        repeated_bug_site_hits: int = 0,
    ) -> float:
        """Map execution signals into a scalar reward used by UCB updates."""
        if not signals:
            return 0.0
        reward = 0.0
        score_component = isinteresting_score
        if score_component is None:
            raw_score = signals.get("isinteresting")
            if isinstance(raw_score, (int, float)):
                score_component = float(raw_score)
        if score_component is not None:
            reward += ISINTERESTING_SCORE_REWARD_WEIGHT * max(
                0.0,
                min(float(score_component), 1.0),
            )
        if bool(signals.get("new_coverage")):
            reward += 1.0
        if bool(signals.get("new_bug")):
            reward += 2.0
        if bool(signals.get("new_bug_site")):
            reward += 0.9
        if bool(signals.get("new_exception_site")):
            reward += 0.6
        if bool(signals.get("new_error_site")):
            reward += 0.5
        if bool(signals.get("new_differential_behavior")):
            reward += 1.0
        rare_edge_score = signals.get("rare_edge_score")
        if isinstance(rare_edge_score, (int, float)):
            reward += 0.75 * max(0.0, min(float(rare_edge_score), 1.0))
        late_parse_depth = signals.get("late_parse_depth")
        if isinstance(late_parse_depth, (int, float)):
            reward += 0.5 * max(0.0, min(float(late_parse_depth), 1.0))
        structure_novelty = signals.get("input_structure_novelty")
        if isinstance(structure_novelty, (int, float)):
            reward += 0.5 * max(0.0, min(float(structure_novelty), 1.0))
        stability_bonus = signals.get("execution_stability_bonus")
        if isinstance(stability_bonus, (int, float)) and reward > 0.0:
            reward += 0.25 * max(0.0, min(float(stability_bonus), 1.0))
        status = str(signals.get("status", "")).lower()
        if bool(signals.get("crash")) or bool(signals.get("timeout")) or status in {
            "crash",
            "timeout",
        }:
            reward += 3.0
        reward += RECENT_NOVELTY_REWARD * max(0.0, min(recent_novelty_rate, 1.0))
        reward -= SAME_COVERAGE_STREAK_PENALTY * math.log1p(
            float(max(0, same_coverage_streak))
        )
        reward -= REPEATED_BUG_SITE_REWARD_ALPHA * math.log1p(
            float(max(0, repeated_bug_site_hits))
        )
        return max(reward, -0.5)

    def _normalize_signals(self, signals: dict[str, Any] | None) -> dict[str, Any] | None:
        """
        Accept either a flat signals dict or a wrapped result shape:
          {"closed_result": {...}, "open_result": {...}}
        and normalize into the flat shape used by UCB bucketing/reward.
        """
        if not signals:
            return signals
        if not isinstance(signals, dict):
            return {"raw_signals": signals}

        if "closed_result" not in signals and "open_result" not in signals:
            return signals

        closed = signals.get("closed_result") or {}
        open_ = signals.get("open_result") or {}

        status = str(closed.get("status") or open_.get("status") or "ok").lower()
        bug_signature = closed.get("bug_signature") or open_.get("bug_signature")

        out: dict[str, Any] = {
            "status": status,
            "bug_signature": bug_signature,
        }

        # Preserve explicit novelty flags if caller computed them.
        for key in (
            "new_coverage",
            "new_bug",
            "new_bug_site",
            "new_exception_site",
            "new_error_site",
            "new_differential_behavior",
            "crash",
            "timeout",
            "coverage_source",
            "new_edge_count",
            "rare_edge_score",
            "parse_category",
            "output_signature",
            "output_class",
            "error_code",
            "diff_behavior_key",
            "diff_pattern_key",
            "mismatch_type_key",
            "structure_key",
            "length_bucket",
            "representative_key",
            "input_structure_novelty",
            "late_parse_depth",
            "partial_parse_success",
            "execution_stability_bonus",
        ):
            if key in signals:
                out[key] = signals[key]
            elif key in closed:
                out[key] = closed[key]
            elif key in open_:
                out[key] = open_[key]

        # Coverage bucketing source (prefer explicit key/signature if provided).
        if signals.get("coverage_key"):
            out["coverage_key"] = signals["coverage_key"]
        elif closed.get("coverage_key"):
            out["coverage_key"] = closed["coverage_key"]
        elif closed.get("coverage_signature"):
            out["coverage_signature"] = closed["coverage_signature"]
        elif closed.get("branch_details_by_file") is not None:
            out["coverage_key"] = {"branch_details_by_file": closed.get("branch_details_by_file")}
        elif (
            "covered_branches" in closed
            or "missing_branches" in closed
            or "covered_branches" in open_
            or "missing_branches" in open_
        ):
            out["coverage_key"] = {
                "covered_branches": closed.get("covered_branches", open_.get("covered_branches")),
                "missing_branches": closed.get("missing_branches", open_.get("missing_branches")),
            }

        # Output signatures if present (for non-bug bucketing fallback).
        for key in ("stdout_signature", "stderr_signature", "semantic_output_signature"):
            if key in closed:
                out[key] = closed[key]
            elif key in open_:
                out[key] = open_[key]

        return out

    def _coverage_bucket_key(self, signals: dict[str, Any] | None) -> str:
        """Derive the coverage bucket key used for the first tree partition."""
        if not signals:
            return "NO_COVERAGE"
        if signals.get("coverage_key"):
            return str(signals["coverage_key"])
        if signals.get("coverage_signature"):
            return str(signals["coverage_signature"])
        if "coverage_bitmap" in signals and signals["coverage_bitmap"] is not None:
            return "COV:" + _short_hash(signals["coverage_bitmap"])
        return "NO_COVERAGE"

    def _bug_bucket_key(self, signals: dict[str, Any] | None) -> str:
        """Derive the bug/output bucket key used for the second tree partition."""
        if not signals:
            return "NO_BUG"
        if signals.get("representative_key"):
            return str(signals["representative_key"])
        if signals.get("bug_key"):
            return str(signals["bug_key"])

        bug_sig = signals.get("bug_signature")
        if isinstance(bug_sig, dict):
            meaningful = {k: v for k, v in bug_sig.items() if v not in (None, "", [], {})}
            if meaningful:
                return "BUG:" + _short_hash(meaningful)

        status = str(signals.get("status", "")).lower()
        if bool(signals.get("crash")) or bool(signals.get("timeout")) or status in {
            "crash",
            "timeout",
        }:
            return "BUG:CRASH_OR_TIMEOUT"

        if signals.get("stdout_signature") or signals.get("stderr_signature"):
            return "OUT:" + _short_hash(
                {
                    "stdout_signature": signals.get("stdout_signature"),
                    "stderr_signature": signals.get("stderr_signature"),
                }
            )
        return "NO_BUG"
