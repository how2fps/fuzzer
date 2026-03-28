from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Iterable
from typing import Any

from isinteresting import get_covered_edges_from_result
from power_scheduler import SeedStats

from core.sqlite_conn import open_results_db

INTERESTING_SCORE_THRESHOLD = 0.5
RECENT_NOVELTY_WINDOW = 16


def _normalize_coverage_key(value: Any) -> str | None:
    if value in (None, "", [], {}):
        return None
    return str(value)


def _compute_recent_novelty_rate(rows: list[sqlite3.Row | tuple[Any, ...]]) -> float:
    if not rows:
        return 0.0
    window = rows[-RECENT_NOVELTY_WINDOW:]
    hits = 0
    for row in window:
        new_coverage = int(row[0] or 0)
        status = str(row[2] or "").strip().lower()
        if new_coverage > 0 or status in {"bug", "crash", "timeout", "error"}:
            hits += 1
    return hits / float(len(window))


def _compute_same_coverage_streak(rows: list[sqlite3.Row | tuple[Any, ...]]) -> int:
    streak = 0
    last_key: str | None = None
    for row in reversed(rows):
        coverage_key = _normalize_coverage_key(row[1])
        new_coverage = int(row[0] or 0)
        if new_coverage > 0:
            break
        if coverage_key is None:
            break
        if last_key is None:
            last_key = coverage_key
            streak = 1
            continue
        if coverage_key != last_key:
            break
        streak += 1
    return streak


def get_seed_stats_from_db(
    conn: sqlite3.Connection,
    corpus: Any,
    target: str,
    scheduler_seeds: list[Any] | None = None,
) -> list[SeedStats]:
    """
    Aggregate runs by seed_id from the DB and return SeedStats keyed by ordinal.
    Includes fuzz_count (times this seed was used), avg_isinteresting_score, bug_count.
    """
    target_set = corpus.maybe_target(target)
    seeds = list(target_set.seeds) if target_set is not None else list(scheduler_seeds or [])
    seed_id_to_ordinal = {s.seed_id: s.ordinal for s in seeds}
    cur = conn.execute(
        """
        SELECT seed_id,
               COUNT(*) AS fuzz_count,
               AVG(isinteresting_score) AS avg_isinteresting_score,
               SUM(CASE WHEN status IN ('bug', 'crash', 'timeout') THEN 1 ELSE 0 END) AS bug_count
        FROM runs
        WHERE target = ?
        GROUP BY seed_id
        """,
        (target,),
    )
    by_seed_id: dict[str, dict[str, Any]] = {}
    for row in cur:
        seed_id, fuzz_count, avg_score, bug_count = row
        by_seed_id[str(seed_id)] = {
            "fuzz_count": fuzz_count or 0,
            "avg_isinteresting_score": float(avg_score) if avg_score is not None else None,
            "bug_count": bug_count or 0,
        }
    history_cur = conn.execute(
        """
        SELECT seed_id, COALESCE(new_coverage, 0), coverage_key, status
        FROM runs
        WHERE target = ?
        ORDER BY id ASC
        """,
        (target,),
    )
    history_by_seed_id: dict[str, list[tuple[Any, ...]]] = {}
    for row in history_cur:
        seed_id = str(row[0])
        history_by_seed_id.setdefault(seed_id, []).append((row[1], row[2], row[3]))
    stats: list[SeedStats] = []
    for seed in seeds:
        row = by_seed_id.get(seed.seed_id, {})
        history = history_by_seed_id.get(seed.seed_id, [])
        stat: SeedStats = {
            "id": seed.ordinal,
            "fuzz_count": row.get("fuzz_count", 0),
            "recent_novelty_rate": _compute_recent_novelty_rate(history),
            "same_coverage_streak": _compute_same_coverage_streak(history),
        }
        if row.get("avg_isinteresting_score") is not None:
            stat["avg_isinteresting_score"] = row["avg_isinteresting_score"]
        if row.get("bug_count", 0) > 0:
            stat["bug_count"] = row["bug_count"]
        stats.append(stat)
    return stats


def seed_stats_for_power_schedule(
    *,
    corpus: Any,
    target: str,
    conn: sqlite3.Connection | None = None,
    scheduler_seeds: list[Any] | None = None,
) -> list[SeedStats]:
    """Build SeedStats for the power scheduler; use DB aggregates when conn is provided."""
    target_set = corpus.maybe_target(target)
    seeds = list(target_set.seeds) if target_set is not None else list(scheduler_seeds or [])
    if conn is None:
        return [{"id": seed.ordinal, "fuzz_count": 0} for seed in seeds]
    return get_seed_stats_from_db(
        conn=conn,
        corpus=corpus,
        target=target,
        scheduler_seeds=scheduler_seeds,
    )


def warmup_power_schedule(
    *,
    corpus: Any,
    target: str,
    power_scheduler_module: Any,
    conn: sqlite3.Connection | None = None,
    scheduler_seeds: list[Any] | None = None,
) -> dict[int, int]:
    stats = seed_stats_for_power_schedule(
        corpus=corpus,
        target=target,
        conn=conn,
        scheduler_seeds=scheduler_seeds,
    )
    if not stats:
        return {}
    schedule = power_scheduler_module.compute_power_schedule(seeds=stats)
    return dict(schedule["seed_energies"])


def init_results_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            iteration INTEGER NOT NULL,
            seed_id TEXT NOT NULL,
            seed_text TEXT,
            mutated_input TEXT NOT NULL,
            generation_time_seconds REAL,
            run_time_seconds REAL,
            status TEXT,
            bug_type TEXT,
            exception TEXT,
            message TEXT,
            file TEXT,
            line INTEGER,
            coverage_key TEXT,
            new_coverage INTEGER,
            isinteresting_score REAL,
            target TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_branches (
            file TEXT NOT NULL,
            from_line INTEGER NOT NULL,
            to_line INTEGER NOT NULL,
            PRIMARY KEY (file, from_line, to_line)
        )
    """)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS added_seed_inputs (
            target TEXT NOT NULL,
            input_text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (target, input_text)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_runs_mutated_input_target
        ON runs (mutated_input, target)
        """
    )
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(runs)").fetchall()
    }
    if "generation_time_seconds" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN generation_time_seconds REAL")
    if "run_time_seconds" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN run_time_seconds REAL")
    if "coverage_key" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN coverage_key TEXT")
    if "new_coverage" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN new_coverage INTEGER")
    conn.commit()


def add_seed_input_if_new(
    conn: sqlite3.Connection,
    *,
    target: str,
    input_text: str,
) -> bool:
    """
    Persist one candidate seed input and return True only on first insert.
    """
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO added_seed_inputs (target, input_text, created_at)
        VALUES (?, ?, ?)
        """,
        (
            target,
            input_text,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    return bool(cur.rowcount and cur.rowcount > 0)


def insert_run(
    conn: sqlite3.Connection,
    *,
    iteration: int,
    seed_id: str,
    seed_text: str,
    mutated_input: str,
    generation_time_seconds: float | None,
    run_time_seconds: float | None,
    status: str | None,
    bug_signature: dict[str, Any] | None,
    coverage_key: str | None,
    new_coverage: bool,
    isinteresting_score: float,
    target: str,
) -> None:
    bug_type = (bug_signature or {}).get("type")
    exc = (bug_signature or {}).get("exception")
    msg = (bug_signature or {}).get("message")
    file_ = (bug_signature or {}).get("file")
    line_raw = (bug_signature or {}).get("line")
    line = int(line_raw) if line_raw is not None and str(
        line_raw).isdigit() else None
    conn.execute(
        """INSERT INTO runs (
            iteration, seed_id, seed_text, mutated_input, generation_time_seconds,
            run_time_seconds, status, bug_type, exception, message, file, line,
            coverage_key, new_coverage,
            isinteresting_score, target, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            iteration,
            seed_id,
            seed_text or "",
            mutated_input,
            generation_time_seconds,
            run_time_seconds,
            status,
            bug_type,
            exc,
            msg,
            file_,
            line,
            coverage_key,
            int(bool(new_coverage)),
            isinteresting_score,
            target,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def input_already_run(
    conn: sqlite3.Connection,
    mutated_input: str,
    target: str,
) -> bool:
    """Return True if this (mutated_input, target) has already been run and recorded in runs."""
    cur = conn.execute(
        "SELECT 1 FROM runs WHERE mutated_input = ? AND target = ? LIMIT 1",
        (mutated_input, target),
    )
    return cur.fetchone() is not None


def insert_seen_branches_into_conn(
    conn: sqlite3.Connection,
    result: dict[str, Any],
) -> None:
    """Insert covered branches from the parser result using an existing connection."""
    insert_seen_edges_into_conn(conn, get_covered_edges_from_result(result))


def insert_seen_edges_into_conn(
    conn: sqlite3.Connection,
    edges: Iterable[tuple[str, int, int]],
) -> None:
    """Insert precomputed covered edges using an existing connection."""
    wrote_any = False
    try:
        for (f, fl, tl) in edges:
            conn.execute(
                "INSERT OR IGNORE INTO seen_branches (file, from_line, to_line) VALUES (?, ?, ?)",
                (f, fl, tl),
            )
            wrote_any = True
        if wrote_any:
            conn.commit()
    except (sqlite3.Error, OSError):
        pass


def insert_seen_branches(db_path: Path | str, result: dict[str, Any]) -> None:
    """Insert covered branches from the parser result into seen_branches."""
    edges = get_covered_edges_from_result(result)
    if not edges:
        return
    path = Path(db_path) if isinstance(db_path, str) else db_path
    if not path.exists():
        return
    try:
        conn = open_results_db(path)
        try:
            insert_seen_branches_into_conn(conn, result)
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        pass


def get_inputs_for_unique_error_line_pairs(
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """
    Return rows for each unique (exception, line) pair that had a bug/crash/timeout,
    with one representative input per pair (seed_id, mutated_input, etc.).
    """
    cur = conn.execute("""
        SELECT exception, line, file, bug_type,
               seed_id, seed_text, mutated_input, status, iteration, isinteresting_score,
               created_at
        FROM runs
        WHERE status IN ('bug', 'crash', 'timeout') AND (exception IS NOT NULL OR line IS NOT NULL)
        ORDER BY exception, line
    """)
    rows = cur.fetchall()
    seen: set[tuple[str | None, int | None]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        (
            exc,
            line,
            file_,
            bug_type,
            seed_id,
            seed_text,
            mutated_input,
            status,
            iteration,
            score,
            created_at,
        ) = row
        key = (exc, line)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "exception": exc,
            "line": line,
            "file": file_,
            "bug_type": bug_type,
            "seed_id": seed_id,
            "seed_text": seed_text,
            "mutated_input": mutated_input,
            "status": status,
            "iteration": iteration,
            "isinteresting_score": score,
            "datetime_executed": created_at,
        })
    return out


def get_run_summary(
    conn: sqlite3.Connection,
    *,
    target: str,
) -> dict[str, Any]:
    total_results = int(
        conn.execute(
            "SELECT COUNT(*) FROM runs WHERE target = ?",
            (target,),
        ).fetchone()[0]
    )
    interesting_results = int(
        conn.execute(
            "SELECT COUNT(*) FROM runs WHERE target = ? AND COALESCE(isinteresting_score, 0) > ?",
            (target, INTERESTING_SCORE_THRESHOLD),
        ).fetchone()[0]
    )

    status_counts = {
        str(status or "unknown"): int(count)
        for status, count in conn.execute(
            """
            SELECT COALESCE(status, 'unknown'), COUNT(*)
            FROM runs
            WHERE target = ?
            GROUP BY COALESCE(status, 'unknown')
            """,
            (target,),
        ).fetchall()
    }

    unique_bug_count = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT DISTINCT
                    COALESCE(status, ''),
                    COALESCE(bug_type, ''),
                    COALESCE(exception, ''),
                    COALESCE(file, ''),
                    COALESCE(line, -1)
                FROM runs
                WHERE target = ? AND COALESCE(status, '') IN ('bug', 'crash', 'timeout', 'error')
            )
            """,
            (target,),
        ).fetchone()[0]
    )

    bug_type_rows = conn.execute(
        """
        SELECT
            COALESCE(NULLIF(bug_type, ''), NULLIF(exception, ''), NULLIF(status, ''), 'unknown') AS label,
            COUNT(*) AS occurrences
        FROM runs
        WHERE target = ? AND COALESCE(status, '') IN ('bug', 'crash', 'timeout', 'error')
        GROUP BY label
        ORDER BY occurrences DESC, label ASC
        LIMIT 8
        """,
        (target,),
    ).fetchall()

    return {
        "total_results": total_results,
        "interesting_results": interesting_results,
        "status_counts": status_counts,
        "unique_bug_count": unique_bug_count,
        "bug_types": [
            {"label": str(label), "count": int(count)}
            for label, count in bug_type_rows
        ],
    }


def _dedupe_text_rows(rows: list[tuple[Any, ...]], *, limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not row:
            continue
        text = row[0]
        if not isinstance(text, str):
            continue
        text = text.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def get_seed_generation_context(
    *,
    conn: sqlite3.Connection,
    corpus: Any,
    target: str,
    include_corpus_seed_fallback: bool = True,
    interesting_limit: int = 10,
    not_interesting_limit: int = 10,
    fuzzed_limit: int = 20,
) -> dict[str, list[str]]:
    target_set = corpus.maybe_target(target)
    top_interesting_rows = conn.execute(
        """
        SELECT mutated_input
        FROM runs
        WHERE target = ? AND isinteresting_score > 0
        ORDER BY isinteresting_score DESC, iteration DESC
        LIMIT ?
        """,
        (target, interesting_limit * 5),
    ).fetchall()
    not_interesting_rows = conn.execute(
        """
        SELECT mutated_input
        FROM runs
        WHERE target = ? AND COALESCE(isinteresting_score, 0) <= 0
        ORDER BY iteration DESC
        LIMIT ?
        """,
        (target, not_interesting_limit * 5),
    ).fetchall()
    already_fuzzed_rows = conn.execute(
        """
        SELECT mutated_input
        FROM runs
        WHERE target = ?
        ORDER BY iteration DESC
        LIMIT ?
        """,
        (target, fuzzed_limit * 5),
    ).fetchall()

    top_interesting = _dedupe_text_rows(top_interesting_rows, limit=interesting_limit)
    not_interesting = _dedupe_text_rows(
        not_interesting_rows,
        limit=not_interesting_limit,
    )
    already_fuzzed = _dedupe_text_rows(already_fuzzed_rows, limit=fuzzed_limit)

    if include_corpus_seed_fallback and target_set is not None and len(already_fuzzed) < fuzzed_limit:
        for seed in target_set.seeds:
            if seed.text not in already_fuzzed:
                already_fuzzed.append(seed.text)
            if len(already_fuzzed) >= fuzzed_limit:
                break

    if include_corpus_seed_fallback and target_set is not None and not top_interesting:
        for seed in target_set.seeds[:interesting_limit]:
            top_interesting.append(seed.text)

    if include_corpus_seed_fallback and not not_interesting:
        fallback = [text for text in already_fuzzed if text not in top_interesting]
        not_interesting.extend(fallback[:not_interesting_limit])

    return {
        "top_interesting_seeds": top_interesting[:interesting_limit],
        "not_interesting_seeds": not_interesting[:not_interesting_limit],
        "already_fuzzed_seeds": already_fuzzed[:fuzzed_limit],
    }
