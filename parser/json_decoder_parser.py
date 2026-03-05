from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import coverage
from coverage.exceptions import NoSource
import json
import os
import sys
import tempfile
import traceback

ROOT_DIR = Path(__file__).resolve().parent.parent
BUGGY_JSON_DIR = ROOT_DIR / "targets" / "json-decoder"
if str(BUGGY_JSON_DIR) not in sys.path:
    sys.path.insert(0, str(BUGGY_JSON_DIR))

from buggy_json import loads


def _path_relative_to_root(path: str | None) -> str | None:
    """Return path relative to ROOT_DIR so edge IDs are stable across machines."""
    if path is None:
        return None
    try:
        return str(Path(path).resolve().relative_to(ROOT_DIR))
    except (ValueError, OSError):
        return path
from buggy_json.decoder_stv import InvalidityBug, JSONDecodeError, PerformanceBug


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
    """
    Collect aggregate branch coverage counts using coverage.py's JSON report.

    Returns:
        {
            "covered_branches": int,
            "missing_branches": int,
        }
    """
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
        # Coverage data can contain paths from another machine (e.g. after
        # loading a .coverage file from elsewhere). When source files are
        # not found, return zeros so the fuzzer can continue.
        return {"covered_branches": 0, "missing_branches": 0}
    finally:
        try:
            os.remove(json_path)
        except OSError:
            pass


def _collect_branch_details_by_file(cov: coverage.Coverage) -> list[dict[str, Any]]:
    """
    Collect per-file branch details including:
    - total executable lines (statements)
    - detailed covered branches (from_line -> to_line)
    - detailed missing branches (from_line -> to_line, -1 means exit)
    """
    out: list[dict[str, Any]] = []
    data = cov.get_data()

    for filename in sorted(data.measured_files()):
        if "buggy_json" not in filename:
            continue
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
                missing_list.append(
                    {
                        "from_line": from_line,
                        "to_line": to_line,
                    }
                )

        covered_list: list[dict[str, int]] = []
        arcs = data.arcs(filename) or []
        for from_line, to_line in arcs:
            if from_line <= 0:
                continue
            covered_list.append(
                {
                    "from_line": from_line,
                    "to_line": to_line,
                }
            )

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


def _log_full_traceback(
    exc: Exception,
    bug_type: str,
    *,
    log_dir: str = "logs",
    filename: str = "tracebacks.log",
) -> None:
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, filename)

    timestamp = datetime.now(timezone.utc)

    with open(log_path, "a", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write(f"Timestamp : {timestamp}\n")
        f.write(f"Bug Type  : {bug_type}\n")
        f.write(f"Exception: {type(exc).__name__}: {exc}\n\n")
        f.write("Traceback:\n")
        f.write("".join(traceback.format_exception(exc)))
        f.write("\n\n")


def _bug_count_to_csv(
    bug_count: dict[tuple[Any, ...], int],
    output_path: str,
) -> None:
    try:
        import pandas as pd
    except ImportError:
        return

    if not bug_count:
        return

    rows: list[dict[str, Any]] = []

    for key, count in bug_count.items():
        bug_type, exc_type, exc_message, filename, lineno = key
        rows.append(
            {
                "bug_type": bug_type,
                "exc_type": exc_type.__name__,
                "exc_message": exc_message,
                "filename": filename,
                "lineno": lineno,
                "count": count,
            }
        )

    new_df = pd.DataFrame(rows)

    if os.path.exists(output_path):
        try:
            existing_df = pd.read_csv(output_path)
            if existing_df.empty:
                combined_df = new_df
            else:
                combined_df = pd.concat([existing_df, new_df], ignore_index=True)
                combined_df = combined_df.groupby(
                    ["bug_type", "exc_type", "exc_message", "filename", "lineno"],
                    as_index=False,
                )["count"].sum()
        except (pd.errors.EmptyDataError, pd.errors.ParserError):
            combined_df = new_df
    else:
        combined_df = new_df

    if not combined_df.empty:
        combined_df.to_csv(output_path, index=False)


def run_json_decoder_with_branches(
    *,
    json_string: str,
    coverage_file: str | None = None,  # ignored: coverage is kept in memory
) -> dict[str, Any]:
    """
    Clone of json_decoder_stv main logic that:
    - runs buggy_json.loads under coverage with branches
    - tracks bug counts and logs tracebacks
    - returns detailed information about uncovered branches

    This is intended to be callable from parser/parser.py so the
    returned data can be embedded directly into the overall result JSON.
    """
    bug_count: dict[tuple[Any, ...], int] = defaultdict(int)

    cov = coverage.Coverage(
        source=["buggy_json"],
        branch=True,
        data_file=None,
    )

    cov.start()

    decoded: Any | None = None
    bug_signature: dict[str, Any] | None = None
    bug_category: str | None = None

    try:
        decoded = loads(json_string)
    except PerformanceBug as exc:
        bug_category = "performance"
        _log_full_traceback(exc, bug_category)
        bug_details = _track_exception(exc)
        bug_signature = {
            "type": bug_category,
            "exception": bug_details.get("exception_type"),
            "message": bug_details.get("message"),
            "file": bug_details.get("file"),
            "line": bug_details.get("line"),
        }
        bug_id = (
            bug_category,
            type(exc),
            str(exc),
            bug_details["file"],
            bug_details["line"],
        )
        bug_count[bug_id] += 1
    except (InvalidityBug, JSONDecodeError) as exc:
        bug_category = "invalidity"
        _log_full_traceback(exc, bug_category)
        bug_details = _track_exception(exc)
        bug_signature = {
            "type": bug_category,
            "exception": bug_details.get("exception_type"),
            "message": bug_details.get("message"),
            "file": bug_details.get("file"),
            "line": bug_details.get("line"),
        }
        bug_id = (
            bug_category,
            type(exc),
            str(exc),
            bug_details["file"],
            bug_details["line"],
        )
        bug_count[bug_id] += 1
    except Exception as exc:
        bug_category = "bonus"
        _log_full_traceback(exc, bug_category)
        bug_details = _track_exception(exc)
        bug_signature = {
            "type": bug_category,
            "exception": bug_details.get("exception_type"),
            "message": bug_details.get("message"),
            "file": bug_details.get("file"),
            "line": bug_details.get("line"),
        }
        bug_id = (
            bug_category,
            type(exc),
            str(exc),
            bug_details["file"],
            bug_details["line"],
        )
        bug_count[bug_id] += 1
    finally:
        cov.stop()

    branch_counts = _collect_branch_counts(cov)
    branch_details_by_file = _collect_branch_details_by_file(cov)

    return {
        "status": "ok" if bug_signature is None else "bug",
        "bug_signature": bug_signature,
        "covered_branches": branch_counts["covered_branches"],
        "missing_branches": branch_counts["missing_branches"],
        "branch_details_by_file": branch_details_by_file,
    }

