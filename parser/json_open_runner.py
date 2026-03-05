from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

import json
import os
import sys


THIS_DIR = Path(__file__).resolve().parent

try:
    # Package-relative import when run as part of the `parser` package
    from .json_decoder_parser import (
        _bug_count_to_csv,
        _log_full_traceback,
        _track_exception,
    )
except ImportError:
    # Fallback for running this file as a standalone script
    if str(THIS_DIR) not in sys.path:
        sys.path.insert(0, str(THIS_DIR))
    from json_decoder_parser import (
        _bug_count_to_csv,
        _log_full_traceback,
        _track_exception,
    )


def run_json_open(*, json_string: str) -> dict[str, Any]:
    bug_count: dict[tuple[Any, ...], int] = defaultdict(int)
    bug_signature: dict[str, Any] | None = None
    bug_category: str | None = None
    decoded: Any | None = None

    try:
        decoded = json.loads(json_string)
    except json.JSONDecodeError as exc:
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

    # logs_dir = "logs"
    # os.makedirs(logs_dir, exist_ok=True)
    # csv_path = os.path.join(logs_dir, "bug_counts_open.csv")
    # _bug_count_to_csv(bug_count, csv_path)

    try:
        json.dumps(decoded)
        decoded_repr: Any = decoded
    except TypeError:
        decoded_repr = repr(decoded)

    return {
        "status": "ok" if bug_signature is None else "bug",
        "bug_signature": bug_signature,
        "decoded": decoded_repr,
    }


def main() -> None:
    data = sys.stdin.read()
    result = run_json_open(json_string=data)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()

