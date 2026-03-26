"""Shared SQLite settings for the fuzzer runs database (multi-process / multi-thread)."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Union


def open_results_db(
    path: Union[Path, str],
    *,
    timeout_seconds: float = 30.0,
) -> sqlite3.Connection:
    """
    Open runs.db with settings that reduce 'database is locked' under concurrent access.

    WAL allows readers (scheduler stats, interestingness) to proceed while a writer
    commits; busy_timeout + connect timeout make writers wait instead of failing fast.
    """
    conn = sqlite3.connect(str(path), timeout=timeout_seconds)
    # Persistent per-database file; safe to run on every open.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    ms = max(1, int(timeout_seconds * 1000))
    conn.execute(f"PRAGMA busy_timeout={ms}")
    return conn
