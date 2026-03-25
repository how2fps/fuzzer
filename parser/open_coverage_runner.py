from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

import coverage
from coverage.exceptions import NoSource

THIS_DIR = Path(__file__).resolve().parent
ROOT_DIR = THIS_DIR.parent

try:
    from .json_open_runner import run_json_open
except ImportError:
    if str(THIS_DIR) not in sys.path:
        sys.path.insert(0, str(THIS_DIR))
    from json_open_runner import run_json_open


def _path_relative_to_root(path: str | None) -> str | None:
    if path is None:
        return None
    try:
        return str(Path(path).resolve().relative_to(ROOT_DIR))
    except (ValueError, OSError):
        return path


def _parse_missing_branches_string(missing_branches: str) -> dict[int, list[int]]:
    by_line: dict[int, list[int]] = {}
    if not missing_branches:
        return by_line
    for from_to_line in missing_branches.split(","):
        from_to_line = from_to_line.strip()
        if not from_to_line:
            continue
        if "-" in from_to_line:
            from_line_str, to_line_str = from_to_line.split("-", 1)
            from_line = int(from_line_str)
            to_line = int(to_line_str)
            by_line.setdefault(from_line, []).append(to_line)
        else:
            from_line = int(from_to_line)
            by_line.setdefault(from_line, []).append(-1)
    return by_line


def _collect_branch_counts(cov: coverage.Coverage) -> dict[str, int]:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".json") as tmp:
        json_path = tmp.name
    try:
        cov.json_report(outfile=json_path)
        with open(json_path, encoding="utf-8") as f:
            report = json.load(f)
        totals = report.get("totals", {})
        num_branches = int(totals.get("num_branches", 0) or 0)
        covered_branches = int(totals.get("covered_branches", 0) or 0)
        missing_branches = max(num_branches - covered_branches, 0)
        return {
            "covered_branches": covered_branches,
            "missing_branches": missing_branches,
        }
    except NoSource:
        return {"covered_branches": 0, "missing_branches": 0}
    finally:
        try:
            os.remove(json_path)
        except OSError:
            pass


def _collect_branch_details_by_file(cov: coverage.Coverage) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    data = cov.get_data()
    for filename in sorted(data.measured_files()):
        try:
            (
                _,
                statements,
                _excluded,
                _missing_lines,
                missing_branches,
            ) = cov.analysis2(filename)
        except coverage.CoverageException:
            continue

        total_lines = len(statements)
        missing_by_line = _parse_missing_branches_string(missing_branches or "")
        missing_list: list[dict[str, int]] = []
        for from_line, targets in sorted(missing_by_line.items()):
            for to_line in sorted(targets):
                missing_list.append({"from_line": from_line, "to_line": to_line})

        covered_list: list[dict[str, int]] = []
        arcs = data.arcs(filename) or []
        for from_line, to_line in arcs:
            if from_line <= 0:
                continue
            covered_list.append({"from_line": from_line, "to_line": to_line})

        out.append(
            {
                "file": _path_relative_to_root(filename),
                "total_lines": total_lines,
                "covered_branches": covered_list,
                "missing_branches": missing_list,
            }
        )
    return out


def _track_exception(exc: Exception) -> dict[str, Any]:
    tb = exc.__traceback__
    frames = traceback.extract_tb(tb)
    last_frame = frames[-1] if frames else None
    return {
        "exception_type": type(exc).__name__,
        "message": str(exc),
        "file": _path_relative_to_root(last_frame.filename) if last_frame else None,
        "line": last_frame.lineno if last_frame else None,
    }


def _run_cidrize_open(*, input_data: bytes) -> dict[str, Any]:
    cidrize_dir = ROOT_DIR / "targets" / "cidrize"
    if str(cidrize_dir) not in sys.path:
        sys.path.insert(0, str(cidrize_dir))
    try:
        from cidrize import CidrizeError, cidrize  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        # When main runs under `uv run`, parser subprocesses may use an
        # interpreter env that does not include target-specific deps. Try to
        # import site-packages from targets/cidrize/.venv explicitly.
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
        from cidrize import CidrizeError, cidrize  # type: ignore[import-not-found]

    input_str = input_data.decode("utf-8", errors="replace").strip()
    bug_signature: dict[str, Any] | None = None
    try:
        cidrize(input_str)
    except CidrizeError as exc:
        details = _track_exception(exc)
        bug_signature = {
            "type": "invalidity",
            "exception": details.get("exception_type"),
            "message": details.get("message"),
            "file": details.get("file"),
            "line": details.get("line"),
        }
    except Exception as exc:
        details = _track_exception(exc)
        bug_signature = {
            "type": "bonus",
            "exception": details.get("exception_type"),
            "message": details.get("message"),
            "file": details.get("file"),
            "line": details.get("line"),
        }
    return {
        "status": "ok" if bug_signature is None else "bug",
        "bug_signature": bug_signature,
    }


def _run_ipyparse_open(*, input_data: bytes) -> dict[str, Any]:
    ipyparse_src = ROOT_DIR / "targets" / "ipyparse" / "src"
    if str(ipyparse_src) not in sys.path:
        sys.path.insert(0, str(ipyparse_src))

    from ipyparse.ipv4 import parse  # type: ignore[import-not-found]

    input_str = input_data.decode("utf-8", errors="replace").strip()
    bug_signature: dict[str, Any] | None = None
    try:
        parse(input_str)
    except Exception as exc:
        details = _track_exception(exc)
        bug_type = "invalidity" if details.get("exception_type") == "ParseException" else "bonus"
        bug_signature = {
            "type": bug_type,
            "exception": details.get("exception_type"),
            "message": details.get("message"),
            "file": details.get("file"),
            "line": details.get("line"),
        }
    return {
        "status": "ok" if bug_signature is None else "bug",
        "bug_signature": bug_signature,
    }


def run_open_target_with_branches(*, target_name: str, input_data: bytes) -> dict[str, Any]:
    if target_name not in {"json_open", "cidrize", "ipyparse"}:
        return {
            "status": "error",
            "bug_signature": None,
            "covered_branches": 0,
            "missing_branches": 0,
            "branch_details_by_file": [],
        }

    cov_source: list[str] | None = None
    if target_name == "json_open":
        cov_source = ["json"]
    elif target_name == "cidrize":
        cov_source = [str(ROOT_DIR / "targets" / "cidrize")]
    elif target_name == "ipyparse":
        cov_source = [str(ROOT_DIR / "targets" / "ipyparse" / "src" / "ipyparse")]

    cov = coverage.Coverage(branch=True, data_file=None, source=cov_source)
    cov.start()
    try:
        if target_name == "json_open":
            input_str = input_data.decode("utf-8", errors="replace")
            open_result = run_json_open(json_string=input_str)
        elif target_name == "cidrize":
            open_result = _run_cidrize_open(input_data=input_data)
        else:
            open_result = _run_ipyparse_open(input_data=input_data)
    finally:
        cov.stop()

    branch_counts = _collect_branch_counts(cov)
    branch_details_by_file = _collect_branch_details_by_file(cov)
    return {
        "status": open_result.get("status", "ok"),
        "bug_signature": open_result.get("bug_signature"),
        "covered_branches": branch_counts["covered_branches"],
        "missing_branches": branch_counts["missing_branches"],
        "branch_details_by_file": branch_details_by_file,
    }
