"""
Parser: run fuzzer input against a selected target and emit normalized JSON results.

Target is a directory with a README describing how to run it. Results include
status and bug signature.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
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

# Target name -> path, run command, and optional oracle target.
# cmd: argv list (relative paths resolved against target dir). Input is appended as final arg
#      unless input_via_stdin is True (then input is passed via stdin).
# From READMEs: cidrize-runner/README, IPv4-IPv6-parser/README, cidrize/README+CLAUDE, json-decoder/README, ipyparse (library).
TARGETS: dict[str, dict[str, Any]] = {
    "cidrize-runner": {
        "path": "cidrize-runner",
        "oracle": "cidrize",
        "cmd_resolver": "_cmd_cidrize_runner",
        "input_via_stdin": False,
        "open": False,
        # Windows binary startup is slow; give it extra headroom.
        "timeout": 25.0,
    },
    "IPv4-IPv6-parser": {
        "path": "IPv4-IPv6-parser",
        "oracle": "ipyparse",
        "cmd_resolver": "_cmd_ipv4_ipv6_parser",
        "input_via_stdin": False,
        "open": False,
    },
    "cidrize": {
        "path": "cidrize",
        "cmd": ["uv", "run", "cidr"],
        "input_via_stdin": False,
        "open": False,
    },
    "ipyparse": {
        "path": "ipyparse",
        "cmd": [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, 'src'); from ipyparse.ipv4 import parse; print(parse(sys.stdin.read().strip()))",
        ],
        "input_via_stdin": True,
        "open": False,
    },
    "json-decoder": {
        "path": "json-decoder",
        "handler": "json_decoder",
        "oracle": "json_open",
        "open": True,
    },
    "json_open": {
        "path": "json-decoder",
        "cmd": [
            sys.executable,
            str(JSON_OPEN_SCRIPT),
        ],
        "input_via_stdin": True,
        "open": False,
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


def _platform_slug() -> str:
    """
    Return a stable slug for selecting target binaries.

    - Windows -> "win"
    - macOS -> "mac"
    - Linux/other -> "linux"
    """
    if sys.platform.startswith("win"):
        return "win"
    if sys.platform == "darwin":
        return "mac"
    # Many CI envs report 'linux' but keep a safe fallback for other POSIX.
    system = platform.system().lower()
    if "linux" in system:
        return "linux"
    return "linux"


def _infer_ip_version(*, input_data: bytes) -> str:
    """
    Best-effort input classification for choosing ipv4 vs ipv6 parser binary.
    """
    try:
        input_str = input_data.decode("utf-8", errors="replace").strip()
    except Exception:
        return "ipv4"
    return "ipv6" if ":" in input_str else "ipv4"


def _cmd_cidrize_runner(*, input_data: bytes, seed_family: str | None = None) -> list[str]:
    slug = _platform_slug()
    exe = f"{slug}-cidrize-runner.exe" if slug == "win" else f"{slug}-cidrize-runner"
    return [f"bin/{exe}", "--func", "cidrize", "--ipstr"]


def _cmd_ipv4_ipv6_parser(*, input_data: bytes, seed_family: str | None = None) -> list[str]:
    slug = _platform_slug()
    if seed_family in {"ipv4", "ipv6"}:
        ip_version = seed_family
    else:
        ip_version = _infer_ip_version(input_data=input_data)
    exe_name = f"{slug}-{ip_version}-parser.exe" if slug == "win" else f"{slug}-{ip_version}-parser"
    return [f"bin/{exe_name}", "--ipstr"]


def _print_run_target_debug(*, target_name: str, argv: list[str], returncode: int | None, stdout: str, stderr: str) -> None:
    print("\n=== run_target debug ===", file=sys.stderr)
    print(f"target={target_name}", file=sys.stderr)
    print(f"argv={argv}", file=sys.stderr)
    if returncode is not None:
        print(f"returncode={returncode}", file=sys.stderr)
    print("--- stdout ---", file=sys.stderr)
    print(stdout.rstrip("\n"), file=sys.stderr)
    print("--- stderr ---", file=sys.stderr)
    print(stderr.rstrip("\n"), file=sys.stderr)
    print("=== /run_target debug ===\n", file=sys.stderr)


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

    # Tooling warnings (e.g. `uv`/venv mismatch) are not target bugs. They often
    # appear as a single "warning: ..." line with no traceback frames.
    stripped_lines = [ln.strip() for ln in stderr.strip().splitlines() if ln.strip()]
    if stripped_lines and not any("Traceback" in ln for ln in stripped_lines):
        last = stripped_lines[-1]
        if last.lower().startswith("warning:"):
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
            if exc_match.group(1).lower() == "warning":
                return out
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
    *,
    seed_family: str | None = None,
    process_cwd: Path | str | None = None,
) -> dict[str, Any]:
    """
    Run one target with the given input. Return result dict with status and
    bug signature.
    Uses hardcoded TARGETS[target_name]["cmd"]; no README parsing.
    """
    entry = TARGETS.get(target_name)
    if not entry:
        return {
            "target": target_name,
            "status": "error",
            "error": f"no hardcoded cmd for target: {target_name}",
            "bug_signature": None,
        }

    cmd: list[str] | None = None
    cmd_resolver_name = entry.get("cmd_resolver")
    if isinstance(cmd_resolver_name, str):
        resolver = globals().get(cmd_resolver_name)
        if callable(resolver):
            cmd = resolver(input_data=input_data, seed_family=seed_family)
    if cmd is None:
        cmd = entry.get("cmd")
    if not isinstance(cmd, list):
        return {
            "target": target_name,
            "status": "error",
            "error": f"no hardcoded cmd for target: {target_name}",
            "bug_signature": None,
        }

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

    returncode: int | None = None
    # Some targets specify a default timeout (e.g. slow Windows process startup).
    # Treat that as a *minimum* timeout rather than overriding the user-configured value.
    entry_timeout = entry.get("timeout")
    effective_timeout = max(float(timeout), float(entry_timeout)) if entry_timeout is not None else float(timeout)
    # Isolated cwd (e.g. per worker scratch) avoids concurrent writes to logs/bug_counts.csv
    # when multiple processes run the same closed target.
    run_cwd = Path(process_cwd).resolve() if process_cwd is not None else target_dir
    if process_cwd is not None:
        run_cwd.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            argv,
            cwd=str(run_cwd),
            input=input_data if input_via_stdin else None,
            capture_output=True,
            timeout=effective_timeout,
        )
        returncode = proc.returncode
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


def _run_open_target_with_coverage(
    *,
    target_name: str,
    target_dir: Path,
    input_data: bytes,
    timeout: float,
) -> dict[str, Any] | None:
    if target_name not in {"json_open", "cidrize", "ipyparse"}:
        return None

    project_root = str(Path(__file__).resolve().parent.parent)
    runner_code = f"""
from __future__ import annotations
import json
import sys
from pathlib import Path

PROJECT_ROOT = r\"{project_root}\"
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from parser.open_coverage_runner import run_open_target_with_branches

def main() -> None:
    data = sys.stdin.buffer.read()
    out = run_open_target_with_branches(target_name={target_name!r}, input_data=data)
    print(json.dumps(out, sort_keys=True, separators=(",", ":")))

if __name__ == "__main__":
    main()
"""
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix="_open_cov_runner.py",
            encoding="utf-8",
            delete=False,
        ) as tmp:
            tmp.write(runner_code)
            temp_path = tmp.name

        proc = subprocess.run(
            [sys.executable, temp_path],
            cwd=str(target_dir),
            input=input_data,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "target": target_name,
            "status": "timeout",
            "bug_signature": None,
            "covered_branches": 0,
            "missing_branches": 0,
            "branch_details_by_file": [],
        }
    except Exception as exc:
        return {
            "target": target_name,
            "status": "error",
            "bug_signature": {
                "type": "exception",
                "exception": type(exc).__name__,
                "message": str(exc),
                "file": None,
                "line": None,
            },
            "covered_branches": 0,
            "missing_branches": 0,
            "branch_details_by_file": [],
        }
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass

    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    payload: dict[str, Any] | None = None
    try:
        parsed = json.loads(stdout)
        payload = parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, TypeError):
        payload = None

    if payload is None:
        bug_sig = _parse_bug_signature(stderr)
        status = "crash" if proc.returncode != 0 else "error"
        if bug_sig.get("type") and status == "error":
            status = "bug"
        return {
            "target": target_name,
            "status": status,
            "bug_signature": bug_sig if bug_sig.get("type") else None,
            "covered_branches": 0,
            "missing_branches": 0,
            "branch_details_by_file": [],
        }

    out: dict[str, Any] = {
        "target": target_name,
        "status": payload.get("status", "ok"),
        "bug_signature": payload.get("bug_signature"),
        "covered_branches": payload.get("covered_branches", 0),
        "missing_branches": payload.get("missing_branches", 0),
        "branch_details_by_file": payload.get("branch_details_by_file", []),
    }
    return out


def run_parser(
    *,
    input_data: bytes | None = None,
    input_path: str | Path | None = None,
    target: str,
    timeout: float = DEFAULT_TIMEOUT,
    print_json: bool = False,
    seed_family: str | None = None,
    enable_open_coverage: bool = False,
    closed_cwd_override: Path | str | None = None,
) -> dict[str, Any]:
    """
    Run fuzzer input against the selected target and return (and optionally print) JSON results.

    Provide exactly one of input_data or input_path. If neither is provided, stdin is read.
    target is the target name (key in TARGETS). For closed targets (cidrize-runner, IPv4-IPv6-parser),
    the equivalent open target is also run and its output is returned separately.

    closed_cwd_override: If set, the closed target subprocess uses this directory as cwd (created if
    needed) so each worker can write logs/bug_counts.csv under a separate tree instead of sharing
    the canonical target folder.

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

        result = run_target(
            target,
            target_dir,
            data,
            timeout=timeout,
            seed_family=seed_family,
            process_cwd=closed_cwd_override,
        )

    # For closed targets, also run the oracle target
    open_name = entry.get("oracle")
    if open_name is not None:
        open_entry = TARGETS.get(open_name)
        open_dir = _TARGETS_BASE / (open_entry["path"] if open_entry and "path" in open_entry else open_name)
        open_dir = open_dir.resolve()
        if open_dir.is_dir():
            open_result = run_target(
                open_name,
                open_dir,
                data,
                timeout=timeout,
                seed_family=seed_family,
            )
            if enable_open_coverage:
                coverage_open_result = _run_open_target_with_coverage(
                    target_name=open_name,
                    target_dir=open_dir,
                    input_data=data,
                    timeout=timeout,
                )
                coverage_payload = {
                    "covered_branches": 0,
                    "missing_branches": 0,
                    "branch_details_by_file": [],
                }
                if isinstance(coverage_open_result, dict):
                    coverage_payload = {
                        "covered_branches": coverage_open_result.get(
                            "covered_branches", 0
                        ),
                        "missing_branches": coverage_open_result.get(
                            "missing_branches", 0
                        ),
                        "branch_details_by_file": coverage_open_result.get(
                            "branch_details_by_file", []
                        ),
                    }
                open_result.update(coverage_payload)
                if isinstance(coverage_open_result, dict):
                    if coverage_open_result.get("status") in {
                        "ok",
                        "bug",
                        "crash",
                        "timeout",
                        "error",
                    }:
                        open_result["status"] = coverage_open_result["status"]
                    if coverage_open_result.get("bug_signature") is not None:
                        open_result["bug_signature"] = coverage_open_result[
                            "bug_signature"
                        ]
            result["open_result"] = open_result
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
