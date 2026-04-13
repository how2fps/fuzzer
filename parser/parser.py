"""
Parser: run fuzzer input against a selected target and emit normalized JSON results.

Target is a directory with a README describing how to run it. Results include
status and bug signature.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from core.behavioral_signals import enrich_execution_result

try:
    from json_decoder_parser import run_json_decoder_with_branches
except ImportError:
    from .json_decoder_parser import run_json_decoder_with_branches

try:
    from qemu_coverage_runner import (
        qemu_coverage_enabled,
        run_target_with_qemu_coverage,
    )
except ImportError:
    from .qemu_coverage_runner import (
        qemu_coverage_enabled,
        run_target_with_qemu_coverage,
    )

try:
    from pyc_coverage_runner import (
        pyc_coverage_supported,
        run_pyc_coverage,
    )
except ImportError:
    from .pyc_coverage_runner import (
        pyc_coverage_supported,
        run_pyc_coverage,
    )

DEFAULT_TIMEOUT = 10.0

COVERAGE_TARGET_NAME = "json_open"

_TARGETS_BASE = Path(__file__).resolve().parent.parent / "targets"

JSON_OPEN_SCRIPT = Path(__file__).resolve().parent / "json_open_runner.py"
IPYPARSE_SCRIPT = Path(__file__).resolve().parent / "ipyparse_runner.py"
TARGETS: dict[str, dict[str, Any]] = {
    "cidrize-runner": {
        "path": "cidrize-runner",
        "oracle": "cidrize",
        "qemu_coverage": {"enabled": True},
        "command": {
            "argv_template": [
                "bin/{platform}-cidrize-runner{exe_suffix}",
                "--func",
                "cidrize",
                "--raise-errors",
                "--ipstr",
            ],
            "input_via_stdin": False,
        },
        "open": False,
        # Windows binary startup is slow; give it extra headroom.
        "timeout": 25.0,
    },
    "ipv4-parser": {
        "path": "IPv4-IPv6-parser",
        "oracle": "ipyparse",
        "qemu_coverage": {"enabled": True},
        "command": {
            "argv_template": [
                "bin/{platform}-ipv4-parser{exe_suffix}",
                "--ipstr",
            ],
            "input_via_stdin": False,
        },
        "open": False,
    },
    "ipv6-parser": {
        "path": "IPv4-IPv6-parser",
        "oracle": "ipyparse",
        "qemu_coverage": {"enabled": True},
        "command": {
            "argv_template": [
                "bin/{platform}-ipv6-parser{exe_suffix}",
                "--ipstr",
            ],
            "input_via_stdin": False,
        },
        "open": False,
    },
    # Legacy combined target that auto-selects the IPv4 vs IPv6 binary.
    "IPv4-IPv6-parser": {
        "path": "IPv4-IPv6-parser",
        "oracle": "ipyparse",
        "qemu_coverage": {"enabled": True},
        "command": {
            "argv_template": [
                "bin/{platform}-{ip_version}-parser{exe_suffix}",
                "--ipstr",
            ],
            "input_via_stdin": False,
        },
        "open": False,
    },
    "cidrize": {
        "path": "cidrize",
        "command": {
            "argv": ["uv", "run", "cidr", "--"],
            "input_via_stdin": False,
        },
        "coverage": {"enabled": True},
        "open": False,
    },
    "ipyparse": {
        "path": "ipyparse",
        "command": {
            "argv": [
                sys.executable,
                str(IPYPARSE_SCRIPT),
            ],
            "input_via_stdin": True,
        },
        "coverage": {"enabled": True},
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
        "command": {
            "argv": [
                sys.executable,
                str(JSON_OPEN_SCRIPT),
            ],
            "input_via_stdin": True,
        },
        "coverage": {"enabled": True},
        "open": False,
    },
}

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


def _deep_merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dicts(merged[key], value)
            continue
        merged[key] = copy.deepcopy(value)
    return merged


def _get_targets_base_dir(*, parser_config: dict[str, Any] | None = None) -> Path:
    if isinstance(parser_config, dict):
        configured_base = parser_config.get("targets_base_dir")
        if isinstance(configured_base, str) and configured_base.strip():
            return Path(configured_base).expanduser().resolve()
    return _TARGETS_BASE.resolve()


def get_target_registry(*, parser_config: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    registry = copy.deepcopy(TARGETS)
    if not isinstance(parser_config, dict):
        return registry
    raw_targets = parser_config.get("targets")
    if not isinstance(raw_targets, dict):
        return registry
    for target_name, raw_entry in raw_targets.items():
        if not isinstance(target_name, str) or not target_name.strip():
            continue
        if not isinstance(raw_entry, dict):
            continue
        base_entry = registry.get(target_name, {})
        if isinstance(base_entry, dict):
            registry[target_name] = _deep_merge_dicts(base_entry, raw_entry)
            continue
        registry[target_name] = copy.deepcopy(raw_entry)
    return registry


def _get_target_path_fragment(*, target_name: str, entry: dict[str, Any]) -> str:
    raw_path = entry.get("path", entry.get("directory", target_name))
    if isinstance(raw_path, str) and raw_path.strip():
        return raw_path
    return target_name


def resolve_target_dir(
    *,
    target: str,
    parser_config: dict[str, Any] | None = None,
) -> Path:
    registry = get_target_registry(parser_config=parser_config)
    entry = registry.get(target, {})
    path_fragment = (
        _get_target_path_fragment(target_name=target, entry=entry)
        if isinstance(entry, dict)
        else target
    )
    return (_get_targets_base_dir(parser_config=parser_config) / path_fragment).resolve()


def _handler_name(entry: dict[str, Any]) -> str | None:
    raw_handler = entry.get("handler")
    if isinstance(raw_handler, str) and raw_handler.strip():
        return raw_handler
    if isinstance(raw_handler, dict):
        handler_kind = raw_handler.get("kind")
        if isinstance(handler_kind, str) and handler_kind.strip():
            return handler_kind
    return None


def _coverage_enabled(entry: dict[str, Any]) -> bool:
    coverage = entry.get("coverage")
    if isinstance(coverage, dict):
        return bool(coverage.get("enabled", False))
    return bool(entry.get("supports_open_coverage", False))


def _command_config(entry: dict[str, Any]) -> dict[str, Any]:
    configured = entry.get("command")
    if isinstance(configured, dict):
        return configured

    legacy: dict[str, Any] = {}
    for key in ("cmd", "argv", "argv_template", "cmd_resolver", "input_via_stdin"):
        if key in entry:
            legacy[key] = entry[key]
    if "append_input_as_final_arg" in entry:
        legacy["append_input_as_final_arg"] = entry["append_input_as_final_arg"]
    return legacy


def _template_context(
    *,
    input_data: bytes,
    seed_family: str | None,
    target_dir: Path,
) -> dict[str, str]:
    platform_slug = _platform_slug()
    ip_version = (
        seed_family
        if seed_family in {"ipv4", "ipv6"}
        else _infer_ip_version(input_data=input_data)
    )
    return {
        "exe_suffix": ".exe" if platform_slug == "win" else "",
        "ip_version": ip_version,
        "parser_dir": str(Path(__file__).resolve().parent),
        "platform": platform_slug,
        "platform_slug": platform_slug,
        "project_root": str(Path(__file__).resolve().parent.parent),
        "python_executable": sys.executable,
        "seed_family": seed_family or "",
        "target_dir": str(target_dir),
    }


def _format_argv_template(
    *,
    argv_template: list[str],
    input_data: bytes,
    seed_family: str | None,
    target_dir: Path,
) -> list[str]:
    context = _template_context(
        input_data=input_data,
        seed_family=seed_family,
        target_dir=target_dir,
    )
    out: list[str] = []
    for part in argv_template:
        out.append(part.format_map(context))
    return out


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

    stripped_lines = [ln.strip() for ln in stderr.strip().splitlines() if ln.strip()]

    def _is_structural_traceback_line(line: str) -> bool:
        return (
            line.startswith("File ")
            or "Traceback" in line
            or line.startswith("During handling of the above exception")
        )

    def _looks_like_exception_name(name: str) -> bool:
        final_segment = name.rsplit(".", 1)[-1].strip()
        return bool(final_segment) and bool(re.match(r"^[A-Z][A-Za-z0-9_]*$", final_segment))

    exc_line_index: int | None = None
    exc_match: re.Match[str] | None = None
    for idx in range(len(stripped_lines) - 1, -1, -1):
        line = stripped_lines[idx]
        if _is_structural_traceback_line(line):
            continue
        candidate = re.match(r"^(\w+(?:\.\w+)*)\s*:\s*(.*)$", line)
        if candidate and candidate.group(1).lower() != "warning" and _looks_like_exception_name(
            candidate.group(1)
        ):
            exc_line_index = idx
            exc_match = candidate
            break

    if exc_match is not None and exc_line_index is not None:
        message_parts = [exc_match.group(2).strip()] if exc_match.group(2).strip() else []
        for cont in stripped_lines[exc_line_index + 1 :]:
            if _is_structural_traceback_line(cont):
                break
            next_match = re.match(r"^(\w+(?:\.\w+)*)\s*:\s*(.*)$", cont)
            if next_match and _looks_like_exception_name(next_match.group(1)):
                break
            message_parts.append(cont)
        out["type"] = "exception"
        out["exception"] = exc_match.group(1)
        out["message"] = "\n".join(message_parts) or None
    else:
        last_line = None
        for line in reversed(stripped_lines):
            if not _is_structural_traceback_line(line):
                last_line = line
                break
        if last_line:
            out["type"] = "message"
            out["message"] = last_line

    return out


def _resolve_argv(
    cmd: list[str],
    target_dir: Path,
    input_arg: str | None,
    append_input_as_final_arg: bool,
) -> list[str]:
    """Resolve relative argv entries against target_dir and append input when configured."""
    argv: list[str] = []
    for part in cmd:
        if not Path(part).is_absolute() and (target_dir / part).exists():
            argv.append(str((target_dir / part).resolve()))
        else:
            argv.append(part)
    if append_input_as_final_arg and input_arg is not None:
        if argv and argv[-1] == "--":
            argv.append(input_arg)
        elif argv and argv[-1].startswith("--") and "=" not in argv[-1]:
            argv[-1] = f"{argv[-1]}={input_arg}"
        else:
            argv.append(input_arg)
    return argv


def _resolve_command(
    *,
    target_name: str,
    entry: dict[str, Any],
    target_dir: Path,
    input_data: bytes,
    seed_family: str | None,
) -> tuple[list[str], bool]:
    command = _command_config(entry)
    input_via_stdin = bool(command.get("input_via_stdin", False))
    append_input_as_final_arg = bool(
        command.get("append_input_as_final_arg", not input_via_stdin)
    )

    cmd: list[str] | None = None
    raw_template = command.get("argv_template")
    if isinstance(raw_template, list) and all(isinstance(part, str) for part in raw_template):
        try:
            cmd = _format_argv_template(
                argv_template=raw_template,
                input_data=input_data,
                seed_family=seed_family,
                target_dir=target_dir,
            )
        except KeyError as exc:
            raise ValueError(
                f"unknown command template field {exc.args[0]!r} for target {target_name!r}"
            ) from exc

    if cmd is None:
        raw_argv = command.get("argv", command.get("cmd"))
        if isinstance(raw_argv, list) and all(isinstance(part, str) for part in raw_argv):
            cmd = list(raw_argv)

    if cmd is None:
        raise ValueError(f"no runnable command configured for target: {target_name}")

    input_arg = None if input_via_stdin else input_data.decode("utf-8", errors="replace")
    argv = _resolve_argv(
        cmd,
        target_dir,
        input_arg=input_arg,
        append_input_as_final_arg=append_input_as_final_arg,
    )
    return (argv, input_via_stdin)


def run_target(
    target_name: str,
    entry: dict[str, Any],
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
    """
    try:
        argv, input_via_stdin = _resolve_command(
            target_name=target_name,
            entry=entry,
            target_dir=target_dir,
            input_data=input_data,
            seed_family=seed_family,
        )
    except ValueError as exc:
        return {
            "target": target_name,
            "status": "error",
            "error": str(exc),
            "bug_signature": None,
        }
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
    run_started_at = time.perf_counter()
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

    bug_sig = _parse_bug_signature(stderr)

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

            status_from_stdout = stdout_obj.get("status")
            if isinstance(status_from_stdout, str):
                result["status"] = status_from_stdout

    result["bug_signature"] = bug_sig

    if bug_sig.get("type") and result.get("status") == "ok":
        result["status"] = "bug"

    stdout_obj: dict[str, Any] | None = None
    try:
        parsed_stdout = json.loads(stdout)
        stdout_obj = parsed_stdout if isinstance(parsed_stdout, dict) else None
    except (json.JSONDecodeError, TypeError):
        stdout_obj = None
    if isinstance(stdout_obj, dict):
        semantic_output = None
        for key in ("semantic_output", "decoded", "parsed"):
            if key in stdout_obj:
                semantic_output = stdout_obj.get(key)
                break
        if semantic_output is not None:
            result["semantic_output"] = semantic_output

    execution_time_seconds = time.perf_counter() - run_started_at
    enrich_execution_result(
        result,
        stdout=stdout,
        stderr=stderr,
        execution_time_seconds=execution_time_seconds,
        returncode=returncode,
    )

    return result


def _run_open_target_with_coverage(
    *,
    target_name: str,
    target_dir: Path,
    input_data: bytes,
    timeout: float,
    ipyparse_family: str | None = None,
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
    out = run_open_target_with_branches(
        target_name={target_name!r},
        input_data=data,
        ipyparse_family={ipyparse_family!r},
    )
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
            "total_branches": 0,
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
            "total_branches": 0,
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
        "total_branches": payload.get("total_branches"),
        "branch_details_by_file": payload.get("branch_details_by_file", []),
    }
    return out


def _infer_oracle_coverage_family(
    *,
    target_name: str,
    oracle_name: str,
    input_data: bytes,
    seed_family: str | None,
) -> str | None:
    if oracle_name != "ipyparse":
        return None
    if target_name == "ipv4-parser":
        return "ipv4"
    if target_name == "ipv6-parser":
        return "ipv6"
    if seed_family in {"ipv4", "ipv6"}:
        return seed_family
    return _infer_ip_version(input_data=input_data)


def _apply_coverage_payload(
    *,
    result: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    details = payload.get("branch_details_by_file")
    has_details = isinstance(details, list) and any(
        isinstance(file_entry, dict) and file_entry.get("covered_branches")
        for file_entry in details
    )
    if has_details or payload.get("covered_branches") is not None or payload.get("missing_branches") is not None:
        for key in (
            "covered_branches",
            "missing_branches",
            "branch_details_by_file",
            "total_branches",
        ):
            if key not in payload:
                continue
            result[key] = payload.get(key)
    for key in ("coverage_backend", "coverage_error", "showmap_returncode"):
        if key not in payload:
            continue
        result[key] = payload.get(key)


def run_parser(
    *,
    input_data: bytes | None = None,
    input_path: str | Path | None = None,
    target: str,
    timeout: float = DEFAULT_TIMEOUT,
    print_json: bool = False,
    seed_family: str | None = None,
    enable_open_coverage: bool = False,
    enable_qemu_coverage: bool = False,
    enable_pyc_coverage: bool = False,
    closed_cwd_override: Path | str | None = None,
    parser_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Run fuzzer input against the selected target and return (and optionally print) JSON results.

    Provide exactly one of input_data or input_path. If neither is provided, stdin is read.
    target is the target name. Built-in targets come from `TARGETS`, and config
    files can override or extend that registry via `parser_config["targets"]`.

    closed_cwd_override: If set, the closed target subprocess uses this directory as cwd (created if
    needed) so each worker can write logs/bug_counts.csv under a separate tree instead of sharing
    the canonical target folder. For json-decoder (in-process handler), this sets the logs directory
    for tracebacks and bug_counts.csv the same way (…/.worker_cwd/wN/logs).

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

    target_registry = get_target_registry(parser_config=parser_config)
    if target not in target_registry:
        out = {"error": f"Unknown target: {target}", "known_targets": list(target_registry)}
        wrapped = {"closed_result": out}
        if print_json:
            print(json.dumps(wrapped), file=sys.stderr)
        return wrapped

    entry = target_registry[target]
    shadow_result: dict[str, Any] | None = None

    handler = _handler_name(entry)
    if handler == "json_decoder":
        input_str = data.decode("utf-8", errors="replace")
        json_log_dir: str | None = None
        if closed_cwd_override is not None:
            scratch_logs = (Path(closed_cwd_override).resolve() / "logs")
            scratch_logs.mkdir(parents=True, exist_ok=True)
            json_log_dir = str(scratch_logs)
        handler_started_at = time.perf_counter()
        json_decoder_info = run_json_decoder_with_branches(
            json_string=input_str,
            log_dir=json_log_dir,
        )
        handler_execution_time_seconds = time.perf_counter() - handler_started_at

        base_result: dict[str, Any] = {
            "target": target,
            "bug_signature": None,
        }
        base_result.update(json_decoder_info)
        enrich_execution_result(
            base_result,
            execution_time_seconds=handler_execution_time_seconds,
        )
        result = base_result
    else:
        target_dir = resolve_target_dir(target=target, parser_config=parser_config)
        if not target_dir.is_dir():
            out = {"error": f"Target directory not found: {target_dir}"}
            wrapped = {"closed_result": out}
            if print_json:
                print(json.dumps(wrapped), file=sys.stderr)
            return wrapped

        result = run_target(
            target,
            entry,
            target_dir,
            data,
            timeout=timeout,
            seed_family=seed_family,
            process_cwd=closed_cwd_override,
        )
        if enable_qemu_coverage and qemu_coverage_enabled(
            entry=entry,
            parser_config=parser_config,
        ):
            try:
                argv, input_via_stdin = _resolve_command(
                    target_name=target,
                    entry=entry,
                    target_dir=target_dir,
                    input_data=data,
                    seed_family=seed_family,
                )
            except ValueError as exc:
                result["coverage_backend"] = "afl-qemu-showmap"
                result["coverage_error"] = str(exc)
                result["total_branches"] = None
            else:
                qemu_payload = run_target_with_qemu_coverage(
                    target_name=target,
                    argv=argv,
                    cwd=(
                        Path(closed_cwd_override).resolve()
                        if closed_cwd_override is not None
                        else target_dir
                    ),
                    input_data=data,
                    input_via_stdin=input_via_stdin,
                    timeout=timeout,
                    entry=entry,
                    parser_config=parser_config,
                )
                if isinstance(qemu_payload, dict):
                    _apply_coverage_payload(result=result, payload=qemu_payload)

        if enable_pyc_coverage and pyc_coverage_supported(target):
            shadow_result = run_pyc_coverage(
                target_name=target,
                input_data=data,
                parser_config=parser_config,
            )

    open_name = entry.get("oracle")
    if open_name is not None:
        open_entry = target_registry.get(open_name)
        open_dir = resolve_target_dir(target=open_name, parser_config=parser_config)
        if open_dir.is_dir():
            open_result = run_target(
                open_name,
                open_entry or {},
                open_dir,
                data,
                timeout=timeout,
                seed_family=seed_family,
            )
            # json-decoder closed path already runs buggy_json under coverage; skip
            # a second coverage subprocess for the json_open oracle.
            if (
                enable_open_coverage
                and handler != "json_decoder"
                and isinstance(open_entry, dict)
                and _coverage_enabled(open_entry)
            ):
                oracle_coverage_family = _infer_oracle_coverage_family(
                    target_name=target,
                    oracle_name=open_name,
                    input_data=data,
                    seed_family=seed_family,
                )
                coverage_open_result = _run_open_target_with_coverage(
                    target_name=open_name,
                    target_dir=open_dir,
                    input_data=data,
                    timeout=timeout,
                    ipyparse_family=oracle_coverage_family,
                )
                coverage_payload = {
                    "covered_branches": 0,
                    "missing_branches": 0,
                    "total_branches": 0,
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
                        "total_branches": coverage_open_result.get(
                            "total_branches", 0
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
                enrich_execution_result(open_result)
            result["open_result"] = open_result
        else:
            result["open_result"] = {
                "target": open_name,
                "status": "error",
                "error": f"Open target directory not found: {open_dir}",
                "bug_signature": None,
            }
            enrich_execution_result(result["open_result"])

    open_result = None
    if isinstance(result, dict) and "open_result" in result:
        open_result = result.pop("open_result")

    wrapped_result: dict[str, Any] = {"closed_result": result}
    if open_result is not None:
        wrapped_result["open_result"] = open_result
    if shadow_result is not None:
        wrapped_result["shadow_result"] = shadow_result

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
    example_print_json()
