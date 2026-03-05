"""
TEMPLATE: Copy to a new file (e.g. custom_parser.py), implement run_parser and
define TARGETS + DEFAULT_TIMEOUT, then in versions/__init__.py add:
    from . import custom_parser
    REGISTRY["custom_parser"] = _as_version(custom_parser)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

# Required exports for the version interface
DEFAULT_TIMEOUT = 10.0
TARGETS: dict[str, dict[str, Any]] = {
    # "target-name": {"path": "...", "cmd": [...], "input_via_stdin": bool, ...}
}


def run_parser(
    *,
    input_data: bytes | None = None,
    input_path: str | Path | None = None,
    target: str,
    timeout: float = DEFAULT_TIMEOUT,
    print_json: bool = False,
) -> dict[str, Any]:
    """
    Run fuzzer input against the selected target. Return dict with at least:
        {"closed_result": {"status": str, "bug_signature": dict?, ...}}
    and optionally "open_result" for oracle comparison.
    """
    # TODO: implement; use TARGETS[target], run subprocess or handler, normalize result
    _ = input_data
    _ = input_path
    _ = target
    _ = timeout
    _ = print_json
    return {"closed_result": {"status": "ok", "bug_signature": None}}
