from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


THIS_DIR = Path(__file__).resolve().parent
ROOT_DIR = THIS_DIR.parent


def _cidrize_dir(*, root_dir: Path | None = None) -> Path:
    base = root_dir if root_dir is not None else ROOT_DIR
    return base / "targets" / "cidrize"


def add_cidrize_import_paths(*, root_dir: Path | None = None) -> Path:
    cidrize_dir = _cidrize_dir(root_dir=root_dir)
    cidrize_dir_str = str(cidrize_dir)
    if cidrize_dir_str not in sys.path:
        sys.path.insert(0, cidrize_dir_str)

    venv_dir = cidrize_dir / ".venv"
    candidates: list[Path] = []
    win_site = venv_dir / "Lib" / "site-packages"
    if win_site.is_dir():
        candidates.append(win_site)

    lib_dir = venv_dir / "lib"
    if lib_dir.is_dir():
        for py_dir in sorted(lib_dir.iterdir()):
            site = py_dir / "site-packages"
            if site.is_dir():
                candidates.append(site)

    for site in candidates:
        site_str = str(site)
        if site_str not in sys.path:
            sys.path.insert(0, site_str)

    return cidrize_dir


def load_cidrize_symbols(*, root_dir: Path | None = None) -> tuple[type[Any], Any]:
    add_cidrize_import_paths(root_dir=root_dir)
    from cidrize import CidrizeError, cidrize  # type: ignore[import-not-found]

    return CidrizeError, cidrize
