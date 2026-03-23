#!/usr/bin/env python3
"""
Re-run results export (runs.csv, unique_error_line_pairs; does not copy bug_counts) and
batch report (report.html / report.json) for an existing results tree.

Usage:
  python rerun_results_export.py results/batch_20260322_093204
  python rerun_results_export.py results/batch_20260322_093204/json_heap/run_1_20260322_083245

Auto-detect:
  - Path contains runs.db at that level → export that run only; if a parent folder
    is named batch_*, also regenerate that batch's report.
  - Otherwise → treat path as a batch root: export every run with runs.db under it,
    then generate_batch_report for that path.

  --report-root DIR   Always run generate_batch_report(batch_folder=DIR) after exports.
  --no-report         Skip generate_batch_report entirely.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.batch_report import generate_batch_report
from core.fuzzer_logging import configure_fuzzer_logging, get_fuzzer_logger
from core.results_export import export_results


def _target_for_run_folder(run_folder: Path, db_path: Path) -> str:
    cfg = run_folder / "config.json"
    if cfg.is_file():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            t = data.get("target")
            if isinstance(t, str) and t.strip():
                return t.strip()
        except (json.JSONDecodeError, OSError):
            pass
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT target FROM runs LIMIT 1").fetchone()
            if row and row[0]:
                return str(row[0])
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    return "unknown"


def _find_batch_ancestor(path: Path) -> Path | None:
    for parent in [path, *path.parents]:
        if parent.name.startswith("batch_"):
            return parent
    return None


def _export_one_run(run_folder: Path, log) -> bool:
    db_path = run_folder / "runs.db"
    if not db_path.is_file():
        return False
    target = _target_for_run_folder(run_folder, db_path)
    log.info("Exporting run folder %s (target=%s)", run_folder, target)
    export_results(
        results_folder=run_folder,
        db_path=db_path,
        target=target,
        copy_bug_counts=False,
    )
    return True


def _export_all_runs_under(root: Path, log) -> int:
    roots = sorted({p.parent for p in root.rglob("runs.db") if p.is_file()})
    n = 0
    for run_folder in roots:
        if _export_one_run(run_folder, log):
            n += 1
    return n


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "folder",
        type=Path,
        help="Batch results directory or a single run directory (contains runs.db)",
    )
    parser.add_argument(
        "--report-root",
        type=Path,
        default=None,
        help="After exports, regenerate report.html for this batch folder",
    )
    parser.add_argument(
        "--no-report",
        action="store_true",
        help="Do not run generate_batch_report",
    )
    args = parser.parse_args()
    folder = args.folder.expanduser().resolve()
    if not folder.is_dir():
        print(f"Not a directory: {folder}", file=sys.stderr)
        return 2

    configure_fuzzer_logging()
    log = get_fuzzer_logger()

    report_target: Path | None = None
    if not args.no_report:
        if args.report_root is not None:
            report_target = args.report_root.expanduser().resolve()
        elif (folder / "runs.db").is_file():
            report_target = _find_batch_ancestor(folder)
        else:
            report_target = folder

    if (folder / "runs.db").is_file():
        if not _export_one_run(folder, log):
            return 1
        if report_target is not None and not report_target.is_dir():
            log.warning("Report root is not a directory: %s", report_target)
            report_target = None
    else:
        count = _export_all_runs_under(folder, log)
        if count == 0:
            log.error("No runs.db found under %s", folder)
            return 1

    if args.no_report:
        return 0

    if report_target is None:
        log.info("No batch report root (use --report-root or a path under batch_*).")
        return 0

    log.info("Generating batch report for %s", report_target)
    out = generate_batch_report(batch_folder=report_target)
    if out is None:
        log.warning("Batch report not written (no runs.csv found under %s).", report_target)
        return 1
    log.info("Wrote %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
