from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_BITMAP_LINE_RE = re.compile(r"^\s*(\d+)\s*:\s*(\d+)\s*$")
_QEMU_COVERAGE_BACKEND = "afl-qemu-showmap"


def _deep_merge_dicts(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dicts(dict(merged[key]), value)
            continue
        merged[key] = value
    return merged


def _normalize_command(value: Any) -> list[str] | None:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        out: list[str] = []
        for part in value:
            if not isinstance(part, str) or not part.strip():
                return None
            out.append(part)
        return out or None
    return None


def _normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    out: list[str] = []
    for part in value:
        if not isinstance(part, str) or not part.strip():
            continue
        out.append(part)
    return out


def get_qemu_coverage_config(
    *,
    entry: Mapping[str, Any] | None = None,
    parser_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    if isinstance(parser_config, Mapping):
        raw_global = parser_config.get("qemu_coverage")
        if isinstance(raw_global, Mapping):
            merged = _deep_merge_dicts(merged, raw_global)
        elif isinstance(raw_global, bool):
            merged["enabled"] = raw_global
    if isinstance(entry, Mapping):
        raw_entry = entry.get("qemu_coverage")
        if isinstance(raw_entry, Mapping):
            merged = _deep_merge_dicts(merged, raw_entry)
        elif isinstance(raw_entry, bool):
            merged["enabled"] = raw_entry
    return merged


def qemu_coverage_enabled(
    *,
    entry: Mapping[str, Any] | None = None,
    parser_config: Mapping[str, Any] | None = None,
) -> bool:
    merged = get_qemu_coverage_config(entry=entry, parser_config=parser_config)
    if "enabled" in merged:
        return bool(merged.get("enabled"))
    return False


def _resolve_showmap_command(config: Mapping[str, Any]) -> list[str]:
    configured = _normalize_command(config.get("showmap_command"))
    if configured:
        return configured

    showmap_path = config.get("showmap_path")
    if isinstance(showmap_path, str) and showmap_path.strip():
        return [showmap_path.strip()]

    env_path = os.environ.get("AFL_SHOWMAP")
    if isinstance(env_path, str) and env_path.strip():
        return [env_path.strip()]

    return ["afl-showmap"]


def _command_available(command: Sequence[str]) -> bool:
    if not command:
        return False
    executable = command[0]
    if not executable:
        return False
    if Path(executable).is_file():
        return True
    return shutil.which(executable) is not None


def _parse_bitmap_indices(path: Path) -> list[int]:
    if not path.is_file():
        return []
    seen: set[int] = set()
    with path.open(encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            match = _BITMAP_LINE_RE.match(raw_line)
            if match is None:
                continue
            try:
                idx = int(match.group(1))
            except ValueError:
                continue
            if idx < 0:
                continue
            seen.add(idx)
    return sorted(seen)


def _coverage_payload_for_slots(*, target_name: str, slots: Sequence[int]) -> dict[str, Any]:
    covered_branches = [
        {"from_line": int(slot), "to_line": int(slot)}
        for slot in slots
        if int(slot) >= 0
    ]
    branch_details = []
    if covered_branches:
        branch_details.append(
            {
                "file": f"qemu_bitmap:{target_name}",
                "covered_branches": covered_branches,
                "missing_branches": [],
            }
        )
    return {
        "status": "ok",
        "bug_signature": None,
        "covered_branches": len(covered_branches),
        "missing_branches": None,
        "branch_details_by_file": branch_details,
        "coverage_backend": _QEMU_COVERAGE_BACKEND,
        "total_branches": None,
    }


def run_target_with_qemu_coverage(
    *,
    target_name: str,
    argv: Sequence[str],
    cwd: Path,
    input_data: bytes | None,
    input_via_stdin: bool,
    timeout: float,
    entry: Mapping[str, Any] | None = None,
    parser_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    config = get_qemu_coverage_config(entry=entry, parser_config=parser_config)
    showmap_command = _resolve_showmap_command(config)
    if not _command_available(showmap_command):
        return {
            "status": "unavailable",
            "bug_signature": None,
            "covered_branches": None,
            "missing_branches": None,
            "branch_details_by_file": [],
            "coverage_backend": _QEMU_COVERAGE_BACKEND,
            "coverage_error": f"showmap command not found: {showmap_command[0]}",
            "total_branches": None,
        }

    timeout_ms = max(1, int(float(timeout) * 1000.0))
    extra_args = _normalize_string_list(config.get("showmap_args"))

    with tempfile.TemporaryDirectory(prefix="qemu_cov_") as tmp_dir:
        bitmap_path = Path(tmp_dir) / "bitmap.txt"
        showmap_argv = list(showmap_command)
        showmap_argv.extend(
            [
                "-Q",
                "-o",
                str(bitmap_path),
                "-m",
                "none",
                "-t",
                str(timeout_ms),
            ]
        )
        showmap_argv.extend(extra_args)
        showmap_argv.append("--")
        showmap_argv.extend(argv)

        try:
            proc = subprocess.run(
                showmap_argv,
                cwd=str(cwd),
                input=input_data if input_via_stdin else None,
                capture_output=True,
                timeout=max(float(timeout) + 1.0, float(timeout) * 2.0),
            )
        except subprocess.TimeoutExpired:
            return {
                "status": "timeout",
                "bug_signature": None,
                "covered_branches": None,
                "missing_branches": None,
                "branch_details_by_file": [],
                "coverage_backend": _QEMU_COVERAGE_BACKEND,
                "coverage_error": "qemu coverage collection timed out",
                "total_branches": None,
            }
        except Exception as exc:
            return {
                "status": "error",
                "bug_signature": None,
                "covered_branches": None,
                "missing_branches": None,
                "branch_details_by_file": [],
                "coverage_backend": _QEMU_COVERAGE_BACKEND,
                "coverage_error": f"{type(exc).__name__}: {exc}",
                "total_branches": None,
            }

        slots = _parse_bitmap_indices(bitmap_path)
        payload = _coverage_payload_for_slots(target_name=target_name, slots=slots)
        payload["showmap_returncode"] = proc.returncode
        if slots:
            return payload

        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            payload["status"] = "error"
            payload["coverage_error"] = stderr or "showmap returned a non-zero exit status"
        return payload


__all__ = [
    "get_qemu_coverage_config",
    "qemu_coverage_enabled",
    "run_target_with_qemu_coverage",
]
