from __future__ import annotations

import shutil
from pathlib import Path

from core.paths import TARGETS_DIR
from parser import TARGETS


def resolve_target_dir(*, target: str) -> Path:
    meta = TARGETS.get(target)
    if not meta:
        return TARGETS_DIR / target
    return TARGETS_DIR / str(meta.get("path", target))


def clear_bug_counts_csv(*, target: str) -> None:
    target_dir = resolve_target_dir(target=target)
    logs_dir = target_dir / "logs"
    csv_path = logs_dir / "bug_counts.csv"
    if not csv_path.is_file():
        return
    csv_path.unlink()


def copy_bug_counts_csv_if_present(*, target: str, results_folder: Path) -> bool:
    target_dir = resolve_target_dir(target=target)
    src = target_dir / "logs" / "bug_counts.csv"
    if not src.is_file():
        return False
    dest = results_folder / "bug_counts.csv"
    shutil.copy2(src, dest)
    return True

