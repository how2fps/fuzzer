from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from isinteresting import get_covered_edges_from_result
from power_scheduler import SeedStats

from core.sqlite_conn import open_results_db


def get_seed_stats_from_db(
    conn: sqlite3.Connection,
    corpus: Any,
    target: str,
) -> list[SeedStats]:
    """
    Aggregate runs by seed_id from the DB and return SeedStats keyed by ordinal.
    Includes fuzz_count (times this seed was used), avg_isinteresting_score, bug_count.
    """
    target_set = corpus.target(target)
    seed_id_to_ordinal = {s.seed_id: s.ordinal for s in target_set.seeds}
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
    stats: list[SeedStats] = []
    for seed in target_set.seeds:
        row = by_seed_id.get(seed.seed_id, {})
        stat: SeedStats = {
            "id": seed.ordinal,
            "fuzz_count": row.get("fuzz_count", 0),
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
) -> list[SeedStats]:
    """Build SeedStats for the power scheduler; use DB aggregates when conn is provided."""
    target_set = corpus.target(target)
    if conn is None:
        return [{"id": seed.ordinal, "fuzz_count": 0} for seed in target_set.seeds]
    return get_seed_stats_from_db(conn=conn, corpus=corpus, target=target)


def warmup_power_schedule(
    *,
    corpus: Any,
    target: str,
    power_scheduler_module: Any,
    conn: sqlite3.Connection | None = None,
) -> dict[int, int]:
    stats = seed_stats_for_power_schedule(
        corpus=corpus, target=target, conn=conn)
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
            status TEXT,
            bug_type TEXT,
            exception TEXT,
            message TEXT,
            file TEXT,
            line INTEGER,
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
    conn.commit()


def insert_run(
    conn: sqlite3.Connection,
    *,
    iteration: int,
    seed_id: str,
    seed_text: str,
    mutated_input: str,
    status: str | None,
    bug_signature: dict[str, Any] | None,
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
            iteration, seed_id, seed_text, mutated_input, status, bug_type,
            exception, message, file, line, isinteresting_score, target, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            iteration,
            seed_id,
            seed_text or "",
            mutated_input,
            status,
            bug_type,
            exc,
            msg,
            file_,
            line,
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
            for (f, fl, tl) in edges:
                conn.execute(
                    "INSERT OR IGNORE INTO seen_branches (file, from_line, to_line) VALUES (?, ?, ?)",
                    (f, fl, tl),
                )
            conn.commit()
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
               seed_id, seed_text, mutated_input, status, iteration, isinteresting_score
        FROM runs
        WHERE status IN ('bug', 'crash', 'timeout') AND (exception IS NOT NULL OR line IS NOT NULL)
        ORDER BY exception, line
    """)
    rows = cur.fetchall()
    seen: set[tuple[str | None, int | None]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        exc, line, file_, bug_type, seed_id, seed_text, mutated_input, status, iteration, score = row
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
        })
    return out


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
    interesting_limit: int = 10,
    not_interesting_limit: int = 10,
    fuzzed_limit: int = 20,
) -> dict[str, list[str]]:
    target_set = corpus.target(target)
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

    if len(already_fuzzed) < fuzzed_limit:
        for seed in target_set.seeds:
            if seed.text not in already_fuzzed:
                already_fuzzed.append(seed.text)
            if len(already_fuzzed) >= fuzzed_limit:
                break

    if not top_interesting:
        for seed in target_set.seeds[:interesting_limit]:
            top_interesting.append(seed.text)

    if not not_interesting:
        fallback = [text for text in already_fuzzed if text not in top_interesting]
        not_interesting.extend(fallback[:not_interesting_limit])

    return {
        "top_interesting_seeds": top_interesting[:interesting_limit],
        "not_interesting_seeds": not_interesting[:not_interesting_limit],
        "already_fuzzed_seeds": already_fuzzed[:fuzzed_limit],
    }
