"""
Parser: run fuzzer input against a selected target and emit normalized JSON results.

Target is a directory with a README describing how to run it. Results include
status and bug signature.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from json_decoder_parser import run_json_decoder_with_branches
except ImportError:
    from .json_decoder_parser import run_json_decoder_with_branches

# Default timeout per target run (seconds)
DEFAULT_TIMEOUT = 10.0

# Name of target that gets coverage bitmap (e.g. json_open)
COVERAGE_TARGET_NAME = "json_open"

# Base path for targets (project root / targets)
_TARGETS_BASE = Path(__file__).resolve().parent.parent / "targets"

# Absolute path to the json_open runner script that uses stdlib json
JSON_OPEN_SCRIPT = Path(__file__).resolve().parent / "json_open_runner.py"

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
        "handler": "json_decoder",
        "open": "json_open",
    },
    "json_open": {
        "path": "json-decoder",
        "cmd": [
            sys.executable,
            str(JSON_OPEN_SCRIPT),
        ],
        "input_via_stdin": True,
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
    Run one target with the given input. Return result dict with status and
    bug signature.
    Uses hardcoded TARGETS[target_name]["cmd"]; no README parsing.
    """
    entry = TARGETS.get(target_name)
    if not entry or "cmd" not in entry:
        return {
            "target": target_name,
            "status": "error",
            "error": f"no hardcoded cmd for target: {target_name}",
            "bug_signature": None,
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
        "bug_signature": None,
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

    # Primary bug signature from stderr (usual case)
    bug_sig = _parse_bug_signature(stderr)

    # If stderr did not yield a bug signature, try to infer it from JSON stdout
    # used by some open targets (e.g. json_open) that encode bug info and status in stdout.
    if not bug_sig.get("type"):
        try:
            stdout_obj = json.loads(stdout)
        except (json.JSONDecodeError, TypeError):
            stdout_obj = None

        if isinstance(stdout_obj, dict):
            bug_info = stdout_obj.get("bug_signature")
            if isinstance(bug_info, dict):
                bug_sig = {
                    "type": bug_info.get("type"),
                    "exception": bug_info.get("exception"),
                    "message": bug_info.get("message"),
                    "file": bug_info.get("file"),
                    "line": str(bug_info.get("line")) if bug_info.get("line") is not None else None,
                }

            # If the JSON stdout includes an explicit status field, trust it.
            status_from_stdout = stdout_obj.get("status")
            if isinstance(status_from_stdout, str):
                result["status"] = status_from_stdout

    result["bug_signature"] = bug_sig

    # If we have a bug signature but status is still "ok", treat it as a bug.
    if bug_sig.get("type") and result.get("status") == "ok":
        result["status"] = "bug"

    return result


def run_parser(
    *,
    input_data: bytes | None = None,
    input_path: str | Path | None = None,
    target: str,
    timeout: float = DEFAULT_TIMEOUT,
    print_json: bool = False,
    coverage_file: str | None = None,
) -> dict[str, Any]:
    """
    Run fuzzer input against the selected target and return (and optionally print) JSON results.

    Provide exactly one of input_data or input_path. If neither is provided, stdin is read.
    target is the target name (key in TARGETS). For closed targets (cidrize-runner, IPv4-IPv6-parser),
    the equivalent open target is also run and its output is returned separately.

    Returns:
        Dict with:
          - "closed_result": the primary target's result dict (status, bug_signature, etc.).
          - "open_result": (optional) the open target's result dict, for targets that
            have an open equivalent.
    """
    if input_path is not None:
        path = Path(input_path)
        if not path.is_file():
            out = {"error": f"Input file not found: {input_path}"}
            wrapped = {"closed_result": out}
            if print_json:
                print(json.dumps(wrapped), file=sys.stderr)
            return wrapped
        data = path.read_bytes()
    elif input_data is not None:
        data = input_data
    else:
        data = sys.stdin.buffer.read()

    if target not in TARGETS:
        out = {"error": f"Unknown target: {target}", "known_targets": list(TARGETS)}
        wrapped = {"closed_result": out}
        if print_json:
            print(json.dumps(wrapped), file=sys.stderr)
        return wrapped

    entry = TARGETS[target]

    # Special handling for json-decoder target using internal helper
    handler = entry.get("handler")
    if handler == "json_decoder":
        input_str = data.decode("utf-8", errors="replace")
        kwargs: dict[str, Any] = {"json_string": input_str}
        if coverage_file is not None:
            kwargs["coverage_file"] = coverage_file
        json_decoder_info = run_json_decoder_with_branches(**kwargs)

        base_result: dict[str, Any] = {
            "target": target,
            "bug_signature": None,
        }
        base_result.update(json_decoder_info)
        result = base_result
    else:
        target_dir = _TARGETS_BASE / entry["path"]
        target_dir = target_dir.resolve()
        if not target_dir.is_dir():
            out = {"error": f"Target directory not found: {target_dir}"}
            wrapped = {"closed_result": out}
            if print_json:
                print(json.dumps(wrapped), file=sys.stderr)
            return wrapped

        result = run_target(target, target_dir, data, timeout=timeout)

    # For closed targets, also run the open equivalent
    open_name = entry.get("open")
    if open_name is not None:
        open_entry = TARGETS.get(open_name)
        open_dir = _TARGETS_BASE / (open_entry["path"] if open_entry and "path" in open_entry else open_name)
        open_dir = open_dir.resolve()
        if open_dir.is_dir():
            result["open_result"] = run_target(open_name, open_dir, data, timeout=timeout)
        else:
            result["open_result"] = {
                "target": open_name,
                "status": "error",
                "error": f"Open target directory not found: {open_dir}",
                "bug_signature": None,
            }

    # Move any open_result out of the closed_result payload to top level.
    open_result = None
    if isinstance(result, dict) and "open_result" in result:
        open_result = result.pop("open_result")

    wrapped_result: dict[str, Any] = {"closed_result": result}
    if open_result is not None:
        wrapped_result["open_result"] = open_result

    if print_json:
        print(json.dumps(wrapped_result, indent=2))
    return wrapped_result


def example_from_bytes() -> None:
    """Run parser with input passed as bytes. No JSON printed; result returned."""
    run_parser(
        input_data=b"192.168.1.0/24",
        target="cidrize-runner",
        timeout=5.0,
        print_json=True,
    )

def example_print_json() -> None:
    """Run parser and print the full result as JSON to stdout."""
    run_parser(
        input_data=b'{"key": "value"',
        target="json-decoder",
        timeout=5.0,
        print_json=True,
    )

if __name__ == "__main__":

    # example_from_bytes()
    
    example_print_json()
