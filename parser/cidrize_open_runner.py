from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any

try:
    from .cidrize_runner_support import ROOT_DIR, load_cidrize_symbols
except ImportError:
    THIS_DIR = Path(__file__).resolve().parent
    if str(THIS_DIR) not in sys.path:
        sys.path.insert(0, str(THIS_DIR))
    from cidrize_runner_support import ROOT_DIR, load_cidrize_symbols


def _path_relative_to_root(path: str | None) -> str | None:
    if path is None:
        return None
    try:
        return str(Path(path).resolve().relative_to(ROOT_DIR))
    except (ValueError, OSError):
        return path


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


def run_cidrize_open(*, input_data: bytes) -> dict[str, Any]:
    CidrizeError, cidrize = load_cidrize_symbols()
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


def main() -> None:
    out = run_cidrize_open(input_data=sys.stdin.buffer.read())
    print(json.dumps(out, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
