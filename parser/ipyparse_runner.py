from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
IPYPARSE_SRC = ROOT_DIR / "targets" / "ipyparse" / "src"
if str(IPYPARSE_SRC) not in sys.path:
    sys.path.insert(0, str(IPYPARSE_SRC))


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


def _infer_ip_version(input_str: str) -> str:
    return "ipv6" if ":" in input_str else "ipv4"


def _parse_ipyparse_input(input_str: str) -> Any:
    if _infer_ip_version(input_str) == "ipv6":
        from ipyparse.ipv6 import IPv6_WholeString  # type: ignore[import-not-found]

        return IPv6_WholeString.parse_string(input_str)[0]

    from ipyparse.ipv4 import IPv4_WholeString  # type: ignore[import-not-found]

    return IPv4_WholeString.parse_string(input_str)[0]


def run_ipyparse(*, input_data: bytes | None = None, input_str: str | None = None) -> dict[str, Any]:
    if input_str is None:
        input_str = (
            input_data.decode("utf-8", errors="replace")
            if input_data is not None
            else sys.stdin.buffer.read().decode("utf-8", errors="replace")
        )
    input_str = input_str.strip()

    try:
        parsed = _parse_ipyparse_input(input_str)
    except Exception as exc:
        details = _track_exception(exc)
        bug_type = (
            "invalidity"
            if details.get("exception_type") == "ParseException"
            else "bonus"
        )
        return {
            "status": "bug",
            "bug_signature": {
                "type": bug_type,
                "exception": details.get("exception_type"),
                "message": details.get("message"),
                "file": details.get("file"),
                "line": details.get("line"),
            },
        }

    return {
        "status": "ok",
        "bug_signature": None,
        "parsed": parsed,
    }


def main() -> None:
    out = run_ipyparse()
    print(json.dumps(out, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
