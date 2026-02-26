"""
Parser: run fuzzer input against a selected target and emit normalized JSON results.

Target is a directory with a README describing how to run it. Results include
status, output signatures, bug signature, semantic output, and coverage bitmap
(for json_open target only).
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

# Default timeout per target run (seconds)
DEFAULT_TIMEOUT = 10.0

# Name of target that gets coverage bitmap (e.g. json_open)
COVERAGE_TARGET_NAME = "json_open"

# Base path for targets (project root / targets)
_TARGETS_BASE = Path(__file__).resolve().parent.parent / "targets"

# Target name -> path, run command, and optional open-equivalent.
# cmd: argv list (relative paths resolved against target dir). Input is appended as final arg
#      unless input_via_stdin is True (then input is passed via stdin).
# From READMEs: cidrize-runner/README, IPv4-IPv6-parser/README, cidrize/README+CLAUDE, json-decoder/README, ipyparse (library).
TARGETS: dict[str, dict[str, Any]] = {
    "cidrize-runner": {
        "path": "cidrize-runner",
        "open": "cidrize",
        "cmd": ["bin/cidrize-runner", "--func", "cidrize", "--ipstr"],
        "input_via_stdin": False,
    },
    "IPv4-IPv6-parser": {
        "path": "IPv4-IPv6-parser",
        "open": "ipyparse",
        "cmd": ["bin/ipv4-parser", "--ipstr"],
        "input_via_stdin": False,
    },
    "cidrize": {
        "path": "cidrize",
        "cmd": ["uv", "run", "cidr"],
        "input_via_stdin": False,
    },
    "ipyparse": {
        "path": "ipyparse",
        "cmd": [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, 'src'); from ipyparse.ipv4 import parse; print(parse(sys.stdin.read().strip()))",
        ],
        "input_via_stdin": True,
    },
    "json-decoder": {
        "path": "json-decoder",
        "cmd": ["uv", "run", "json_decoder_stv.py", "--str-json"],
        "input_via_stdin": False,
    },
}

# Patterns to normalize for stable hashes (paths, numbers, timestamps, PIDs)
NORMALIZE_PATTERNS = [
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[\sT]\d{2}:\d{2}:\d{2}[.\d]*Z?", re.I), "<TIMESTAMP>"),
    (re.compile(r"\b\d{10,}\b"), "<NUM>"),
    (re.compile(r"0x[0-9a-fA-F]+"), "<HEX>"),
    (re.compile(r'File "[^"]*", line \d+'), 'File "<PATH>", line <LINE>'),
    (re.compile(r'"[^"]*[/\\][^"]*"'), '"<PATH>"'),
    (re.compile(r"\b(line \d+)", re.I), r"<LINE>"),
]


def _normalize_text(text: str) -> str:
    """Normalize text for stable hashing by replacing variable parts."""
    if not text:
        return ""
    out = text.strip()
    for pat, repl in NORMALIZE_PATTERNS:
        out = pat.sub(repl, out)
    return out


def _signature(text: str) -> str:
    """Return a normalized hash (hex) of text."""
    normalized = _normalize_text(text)
    return hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()[:16]


def _parse_bug_signature(stderr: str) -> dict[str, Any]:
    """Extract bug signature: type, exception, message, file:line from stderr."""
    out: dict[str, Any] = {
        "type": None,
        "exception": None,
        "message": None,
        "file": None,
        "line": None,
    }
    if not stderr:
        return out

    # Traceback file/line: use last frame (where exception was raised)
    file_line_matches = list(
        re.finditer(
            r'File\s+"([^"]+)",\s*line\s+(\d+)',
            stderr,
            re.MULTILINE | re.IGNORECASE,
        )
    )
    if file_line_matches:
        m = file_line_matches[-1]
        out["file"] = m.group(1)
        out["line"] = m.group(2)

    # Last line often: ExceptionType: message
    last_line = None
    for line in reversed(stderr.strip().splitlines()):
        line = line.strip()
        if line and not line.startswith("File ") and "Traceback" not in line:
            last_line = line
            break
    if last_line:
        exc_match = re.match(r"^(\w+(?:\.\w+)*)\s*:\s*(.*)$", last_line)
        if exc_match:
            out["type"] = "exception"
            out["exception"] = exc_match.group(1)
            out["message"] = exc_match.group(2).strip() or None
        else:
            out["type"] = "message"
            out["message"] = last_line

    return out


def _semantic_output(stdout: str, stderr: str) -> str | None:
    """Produce a normalized semantic representation of program output."""
    combined = (stdout or "") + "\n" + (stderr or "")
    if not combined.strip():
        return None
    normalized = _normalize_text(combined)
    # Optional: try to parse as JSON and re-serialize for JSON targets
    try:
        obj = json.loads(stdout or "{}")
        return json.dumps(obj, sort_keys=True, separators=(",", ":"))
    except (json.JSONDecodeError, TypeError):
        pass
    return normalized[:2000]  # cap size


def _read_coverage_bitmap(csv_path: Path) -> list[int] | None:
    """Read coverage bitmap from a CSV (e.g. one column of 0/1 or edge counts)."""
    if not csv_path.is_file():
        return None
    try:
        text = csv_path.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            return None
        lines = text.splitlines()
        if not lines:
            return None
        # Assume last column or first column might be bitmap; support single column of ints
        out: list[int] = []
        for line in lines:
            parts = [p.strip() for p in line.split(",")]
            for p in parts:
                try:
                    out.append(int(p))
                except ValueError:
                    pass
        return out if out else None
    except OSError:
        return None


def _resolve_argv(cmd: list[str], target_dir: Path, input_str: str | None, input_via_stdin: bool) -> list[str]:
    """Build argv from hardcoded cmd; resolve relative paths; append input unless input_via_stdin."""
    argv: list[str] = []
    for part in cmd:
        if not Path(part).is_absolute() and (target_dir / part).exists():
            argv.append(str((target_dir / part).resolve()))
        else:
            argv.append(part)
    if not input_via_stdin and input_str is not None:
        argv.append(input_str)
    return argv


def run_target(
    target_name: str,
    target_dir: Path,
    input_data: bytes,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """
    Run one target with the given input. Return result dict with status,
    stdout/stderr, signatures, bug signature, semantic output, and optionally coverage.
    Uses hardcoded TARGETS[target_name]["cmd"]; no README parsing.
    """
    entry = TARGETS.get(target_name)
    if not entry or "cmd" not in entry:
        return {
            "target": target_name,
            "status": "error",
            "error": f"no hardcoded cmd for target: {target_name}",
            "stdout_signature": None,
            "stderr_signature": None,
            "bug_signature": None,
            "semantic_output": None,
            "coverage_bitmap": None,
        }

    cmd = entry["cmd"]
    input_via_stdin = entry.get("input_via_stdin", False)
    input_str = input_data.decode("utf-8", errors="replace") if not input_via_stdin else None
    argv = _resolve_argv(cmd, target_dir, input_str, input_via_stdin)
    if not argv:
        argv = [sys.executable, "-c", "pass"]

    result: dict[str, Any] = {
        "target": target_name,
        "status": "ok",
        "stdout_signature": None,
        "stderr_signature": None,
        "bug_signature": None,
        "semantic_output": None,
        "coverage_bitmap": None,
    }

    try:
        proc = subprocess.run(
            argv,
            cwd=str(target_dir),
            input=input_data if input_via_stdin else None,
            capture_output=True,
            timeout=timeout,
        )
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired as e:
        result["status"] = "timeout"
        stdout = (e.stdout or b"").decode("utf-8", errors="replace")
        stderr = (e.stderr or b"").decode("utf-8", errors="replace")
    except Exception as e:
        result["status"] = "crash"
        stdout = ""
        stderr = str(e)
    else:
        if proc.returncode != 0:
            result["status"] = "crash"
        result["exit_code"] = proc.returncode

    result["stdout_signature"] = _signature(stdout)
    result["stderr_signature"] = _signature(stderr)
    result["bug_signature"] = _parse_bug_signature(stderr)
    result["semantic_output"] = _semantic_output(stdout, stderr)

    # Coverage bitmap only for json_open target (CSV written by run into target_dir)
    if COVERAGE_TARGET_NAME in target_name.lower():
        for csv_name in ("coverage.csv", "coverage_bitmap.csv", "edges.csv", "output.csv"):
            csv_path = target_dir / csv_name
            if csv_path.is_file():
                result["coverage_bitmap"] = _read_coverage_bitmap(csv_path)
                break

    return result


def run_parser(
    *,
    input_data: bytes | None = None,
    input_path: str | Path | None = None,
    target: str,
    timeout: float = DEFAULT_TIMEOUT,
    print_json: bool = False,
) -> dict[str, Any]:
    """
    Run fuzzer input against the selected target and return (and optionally print) JSON results.

    Provide exactly one of input_data or input_path. If neither is provided, stdin is read.
    target is the target name (key in TARGETS). For closed targets (cidrize-runner, IPv4-IPv6-parser),
    the equivalent open target is also run and its output is in the "open_result" nested dict.

    Returns:
        Result dict (status, signatures, bug_signature, semantic_output, coverage_bitmap).
        If target has an open equivalent, includes "open_result" with the open target's output.
    """
    if input_path is not None:
        path = Path(input_path)
        if not path.is_file():
            out = {"error": f"Input file not found: {input_path}"}
            if print_json:
                print(json.dumps(out), file=sys.stderr)
            return out
        data = path.read_bytes()
    elif input_data is not None:
        data = input_data
    else:
        data = sys.stdin.buffer.read()

    if target not in TARGETS:
        out = {"error": f"Unknown target: {target}", "known_targets": list(TARGETS)}
        if print_json:
            print(json.dumps(out), file=sys.stderr)
        return out

    entry = TARGETS[target]
    target_dir = _TARGETS_BASE / entry["path"]
    target_dir = target_dir.resolve()
    if not target_dir.is_dir():
        out = {"error": f"Target directory not found: {target_dir}"}
        if print_json:
            print(json.dumps(out), file=sys.stderr)
        return out

    result = run_target(target, target_dir, data, timeout=timeout)

    # For closed targets, also run the open equivalent and nest its output
    open_name = entry.get("open")
    if open_name is not None:
        open_dir = _TARGETS_BASE / open_name
        open_dir = open_dir.resolve()
        if open_dir.is_dir():
            result["open_result"] = run_target(open_name, open_dir, data, timeout=timeout)
        else:
            result["open_result"] = {
                "target": open_name,
                "status": "error",
                "error": f"Open target directory not found: {open_dir}",
                "stdout_signature": None,
                "stderr_signature": None,
                "bug_signature": None,
                "semantic_output": None,
                "coverage_bitmap": None,
            }

    if print_json:
        print(json.dumps(result, indent=2))
    return result


def example_from_bytes() -> None:
    """Run parser with input passed as bytes. No JSON printed; result returned."""
    result = run_parser(
        input_data=b"192.168.1.0/24",
        target="cidrize-runner",
        timeout=5.0,
        print_json=True,
    )
    print("Example 1 (bytes, cidrize-runner): status =", result.get("status"))

def example_print_json() -> None:
    """Run parser and print the full result as JSON to stdout."""
    run_parser(
        input_data=b'{"key": "value"}',
        target="json-decoder",
        timeout=5.0,
        print_json=True,
    )

if __name__ == "__main__":

    # example_from_bytes()
    
    example_print_json()
