from __future__ import annotations

import csv
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any

from core.paths import TARGETS_DIR
from parser import TARGETS


def resolve_target_dir(*, target: str) -> Path:
    meta = TARGETS.get(target)
    if not meta:
        return TARGETS_DIR / target
    return TARGETS_DIR / str(meta.get("path", target))


def _merge_worker_bug_counts_csvs(
    source_paths: list[Path],
    dest: Path,
) -> bool:
    """
    Merge bug_counts.csv files from worker scratch dirs into dest.
    Rows are keyed by (bug_type, exc_type, exc_message, filename, lineno); counts are summed.
    """
    if not source_paths:
        return False
    counts: defaultdict[tuple[str, str, str, str, str], int] = defaultdict(int)
    fieldnames: list[str] | None = None
    for src in source_paths:
        if not src.is_file():
            continue
        with src.open(newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames:
                fieldnames = list(reader.fieldnames)
            for row in reader:
                if not row:
                    continue
                try:
                    c = int(str(row.get("count", "0")).strip() or "0")
                except ValueError:
                    c = 0
                key = (
                    str(row.get("bug_type", "") or ""),
                    str(row.get("exc_type", "") or ""),
                    str(row.get("exc_message", "") or ""),
                    str(row.get("filename", "") or ""),
                    str(row.get("lineno", "") or ""),
                )
                counts[key] += c
    if not counts or not fieldnames:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Ensure expected columns for cidrize-style CSVs
    keys_order = (
        "bug_type",
        "exc_type",
        "exc_message",
        "filename",
        "lineno",
        "count",
    )
    out_fields = [k for k in keys_order if k in fieldnames] or fieldnames
    with dest.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=out_fields, extrasaction="ignore")
        w.writeheader()
        for key, total in sorted(counts.items()):
            row: dict[str, Any] = {
                "bug_type": key[0],
                "exc_type": key[1],
                "exc_message": key[2],
                "filename": key[3],
                "lineno": key[4],
                "count": total,
            }
            w.writerow(row)
    return True


def clear_bug_counts_csv(
    *, target: str, results_folder: Path | None = None
) -> None:
    target_dir = resolve_target_dir(target=target)
    logs_dir = target_dir / "logs"
    csv_path = logs_dir / "bug_counts.csv"
    if csv_path.is_file():
        csv_path.unlink()
    if results_folder is not None:
        scratch = results_folder / ".worker_cwd"
        if scratch.is_dir():
            shutil.rmtree(scratch, ignore_errors=True)


def copy_bug_counts_csv_if_present(*, target: str, results_folder: Path) -> bool:
    """
    Prefer merged worker scratch bug_counts under results_folder/.worker_cwd/w*/logs/,
    else copy from the canonical target logs/bug_counts.csv.
    """
    worker_csvs = sorted(results_folder.glob(".worker_cwd/w*/logs/bug_counts.csv"))
    dest = results_folder / "bug_counts.csv"
    if worker_csvs and _merge_worker_bug_counts_csvs(worker_csvs, dest):
        return True
    target_dir = resolve_target_dir(target=target)
    src = target_dir / "logs" / "bug_counts.csv"
    if not src.is_file():
        return False
    shutil.copy2(src, dest)
    return True
