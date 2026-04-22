from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from collections.abc import Mapping, Sequence
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

try:
    from .ipyparse_runner import run_ipyparse
except ImportError:
    if str(THIS_DIR) not in sys.path:
        sys.path.insert(0, str(THIS_DIR))
    from ipyparse_runner import run_ipyparse


def _path_relative_to_root(path: str | None) -> str | None:
    if path is None:
        return None
    try:
        return str(Path(path).resolve().relative_to(ROOT_DIR))
    except (ValueError, OSError):
        return path


def _load_branch_report(cov: coverage.Coverage) -> dict[str, Any] | None:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".json") as tmp:
        json_path = tmp.name
    try:
        cov.json_report(outfile=json_path)
        with open(json_path, encoding="utf-8") as f:
            report = json.load(f)
        if isinstance(report, dict):
            return report
    except NoSource:
        return None
    finally:
        try:
            os.remove(json_path)
        except OSError:
            pass
    return None


def _coerce_branch_list(raw_pairs: object) -> list[dict[str, int]]:
    branches: list[dict[str, int]] = []
    if not isinstance(raw_pairs, Sequence):
        return branches
    for pair in raw_pairs:
        if not isinstance(pair, Sequence) or len(pair) != 2:
            continue
        try:
            from_line = int(pair[0])
            to_line = int(pair[1])
        except (TypeError, ValueError):
            continue
        if from_line <= 0:
            continue
        branches.append({"from_line": from_line, "to_line": to_line})
    return branches


def _infer_ipyparse_family(*, input_data: bytes, ipyparse_family: str | None) -> str:
    if ipyparse_family in {"ipv4", "ipv6"}:
        return ipyparse_family
    try:
        input_str = input_data.decode("utf-8", errors="replace").strip()
    except Exception:
        return "ipv4"
    return "ipv6" if ":" in input_str else "ipv4"


def _ipyparse_coverage_config(
    *,
    input_data: bytes,
    ipyparse_family: str | None,
) -> dict[str, object]:
    package_dir = ROOT_DIR / "targets" / "ipyparse" / "src" / "ipyparse"
    family = _infer_ipyparse_family(
        input_data=input_data,
        ipyparse_family=ipyparse_family,
    )
    config: dict[str, object] = {
        "omit": [str(package_dir / "test" / "*")],
    }
    module_path = package_dir / f"{family}.py"
    if module_path.is_file():
        config["include"] = [str(module_path)]
    return config


def _collect_branch_counts(report: Mapping[str, Any] | None) -> dict[str, int]:
    if report is None:
        return {
            "covered_branches": 0,
            "missing_branches": 0,
            "total_branches": 0,
        }
    totals = report.get("totals")
    if not isinstance(totals, Mapping):
        totals = {}
    num_branches = int(totals.get("num_branches", 0) or 0)
    covered_branches = int(totals.get("covered_branches", 0) or 0)
    missing_branches = max(num_branches - covered_branches, 0)
    return {
        "covered_branches": covered_branches,
        "missing_branches": missing_branches,
        "total_branches": num_branches,
    }


def _collect_branch_details_by_file(report: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if report is None:
        return out
    files = report.get("files")
    if not isinstance(files, Mapping):
        return out
    for filename, file_report in sorted(files.items()):
        if not isinstance(file_report, Mapping):
            continue
        summary = file_report.get("summary")
        if not isinstance(summary, Mapping):
            summary = {}
        total_lines = int(summary.get("num_statements", 0) or 0)
        covered_list = _coerce_branch_list(file_report.get("executed_branches"))
        missing_list = _coerce_branch_list(file_report.get("missing_branches"))

        out.append(
            {
                "file": _path_relative_to_root(filename),
                "total_lines": total_lines,
                "covered_branches": covered_list,
                "missing_branches": missing_list,
            }
        )
    return out


def _collect_executed_arcs_by_file(cov: coverage.Coverage) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        measured_files = sorted(cov.get_data().measured_files())
    except Exception:
        return out

    for filename in measured_files:
        arcs = _coerce_branch_list(cov.get_data().arcs(filename))
        if not arcs:
            continue
        out.append(
            {
                "file": _path_relative_to_root(filename),
                "executed_arcs": arcs,
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
    result = run_ipyparse(input_data=input_data)
    return {
        "status": result.get("status", "ok"),
        "bug_signature": result.get("bug_signature"),
    }


def run_open_target_with_branches(
    *,
    target_name: str,
    input_data: bytes,
    ipyparse_family: str | None = None,
) -> dict[str, Any]:
    if target_name not in {"json_open", "cidrize", "ipyparse"}:
        return {
            "status": "error",
            "bug_signature": None,
            "covered_branches": 0,
            "missing_branches": 0,
            "branch_details_by_file": [],
        }

    cov_kwargs: dict[str, object] = {"branch": True, "data_file": None}
    if target_name == "json_open":
        cov_kwargs["source"] = ["json"]
    elif target_name == "cidrize":
        cov_kwargs["source"] = [str(ROOT_DIR / "targets" / "cidrize")]
    elif target_name == "ipyparse":
        cov_kwargs.update(
            _ipyparse_coverage_config(
                input_data=input_data,
                ipyparse_family=ipyparse_family,
            )
        )

    cov = coverage.Coverage(**cov_kwargs)
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

    branch_report = _load_branch_report(cov)
    branch_counts = _collect_branch_counts(branch_report)
    branch_details_by_file = _collect_branch_details_by_file(branch_report)
    executed_arcs_by_file = _collect_executed_arcs_by_file(cov)
    return {
        "status": open_result.get("status", "ok"),
        "bug_signature": open_result.get("bug_signature"),
        "covered_branches": branch_counts["covered_branches"],
        "missing_branches": branch_counts["missing_branches"],
        "total_branches": branch_counts["total_branches"],
        "branch_details_by_file": branch_details_by_file,
        "executed_arcs_by_file": executed_arcs_by_file,
    }

def refill_from_uncovered(history: list[str], rng: random.Random) -> tuple[str, ...]:
    all_items = tuple(grammar_ast.available_coverage_items(mutator_kind="grammar"))

    seen: set[str] = set()
    for text in history:
        # Union coverage from all previously seen seeds.
        seen.update(ast_cov(text))

    missing = [item for item in all_items if item not in seen]

    out: list[str] = []
    for item in missing:
        # Bias generation toward AST nodes / productions we have not hit yet.
        gen = grammar_ast.generate_from_rule(
            start_rule="start",
            rng=rng,
            count=1,
            min_mutation_rounds=0,
            max_mutation_rounds=0,
            preferred_coverage_items=[item],
            mutator_kind="grammar",
        )
        if gen:
            out.append(gen[0])

    return tuple(out)
