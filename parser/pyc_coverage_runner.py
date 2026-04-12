from __future__ import annotations

import dis
import importlib
import inspect
import sys
import types
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent

DEFAULT_RELATIVE_PYC_SUBDIRS: dict[str, str] = {
    "cidrize-runner": "cidrize-runner/pyc",
    "ipv4-parser": "ipv4-parser/pyc",
    "ipv6-parser": "ipv6-parser/pyc",
}

DEFAULT_PYC_ROOTS: dict[str, Path] = {
    key: ROOT_DIR / "blackbox_disam" / "extracted_pyc" / rel
    for key, rel in DEFAULT_RELATIVE_PYC_SUBDIRS.items()
}

DEFAULT_MODULES: dict[str, list[str]] = {
    "cidrize": ["buggy_cidrize.cidrize_stv"],
    "ipv4": ["buggy_ipyparse.ipv4_stv", "buggy_ipyparse.ipv4_mstv"],
    "ipv6": ["buggy_ipyparse.ipv6_mstv", "buggy_ipyparse.ipv6_stv"],
}

_UNCONDITIONAL_JUMPS = {
    "JUMP_ABSOLUTE",
    "JUMP_FORWARD",
    "JUMP_BACKWARD",
    "JUMP_BACKWARD_NO_INTERRUPT",
    "JUMP_NO_INTERRUPT",
    "JUMP",
}
_TERMINATORS = {
    "RETURN_VALUE",
    "RETURN_CONST",
    "RAISE_VARARGS",
    "RERAISE",
    "END_ASYNC_FOR",
    "END_FINALLY",
}


def pyc_coverage_supported(target_name: str) -> bool:
    return target_name in {
        "cidrize-runner",
        "ipv4-parser",
        "ipv6-parser",
        "IPv4-IPv6-parser",
    }


def _infer_ip_version(input_data: bytes) -> str:
    try:
        input_str = input_data.decode("utf-8", errors="replace").strip()
    except Exception:
        return "ipv4"
    return "ipv6" if ":" in input_str else "ipv4"


def _iter_code_objects(code: types.CodeType) -> Iterable[types.CodeType]:
    stack = [code]
    while stack:
        current = stack.pop()
        yield current
        for const in current.co_consts:
            if isinstance(const, types.CodeType):
                stack.append(const)


def _collect_line_numbers(code: types.CodeType) -> set[int]:
    lines: set[int] = set()
    try:
        for _, _, line in code.co_lines():
            if line:
                lines.add(int(line))
        return lines
    except AttributeError:
        pass
    for _, line in dis.findlinestarts(code):
        if line:
            lines.add(int(line))
    return lines


def _build_offset_edges(code: types.CodeType) -> set[tuple[int, int]]:
    instructions = list(dis.get_instructions(code))
    if not instructions:
        return set()
    edges: set[tuple[int, int]] = set()
    total_instructions = len(instructions)
    for index, instr in enumerate(instructions):
        src = int(instr.offset) + 1
        next_offset = None
        if index + 1 < total_instructions:
            next_offset = int(instructions[index + 1].offset) + 1

        if instr.opcode in dis.hasjrel or instr.opcode in dis.hasjabs:
            target = instr.argval if isinstance(instr.argval, int) else None
            if target is not None:
                edges.add((src, int(target) + 1))
            if instr.opname not in _UNCONDITIONAL_JUMPS and next_offset is not None:
                edges.add((src, next_offset))
            continue

        if instr.opname in _TERMINATORS:
            continue
        if next_offset is not None:
            edges.add((src, next_offset))
    return edges


def _line_map_for_code(code: types.CodeType) -> dict[int, int]:
    line_by_offset: dict[int, int] = {}
    last_line: int | None = None
    for instr in dis.get_instructions(code):
        if instr.starts_line is not None:
            last_line = int(instr.starts_line)
        if last_line is not None:
            line_by_offset[int(instr.offset) + 1] = last_line
    return line_by_offset


def _offset_edges_to_line_edges(
    edges: set[tuple[int, int]],
    line_by_offset: dict[int, int],
) -> set[tuple[int, int]]:
    line_edges: set[tuple[int, int]] = set()
    for src, dst in edges:
        from_line = line_by_offset.get(src)
        to_line = line_by_offset.get(dst)
        if not from_line or not to_line:
            continue
        if from_line == to_line:
            continue
        line_edges.add((from_line, to_line))
    return line_edges


def _branch_edge_set(edges: set[tuple[int, int]]) -> set[tuple[int, int]]:
    exits: dict[int, set[int]] = {}
    for from_line, to_line in edges:
        if from_line <= 0 or to_line <= 0:
            continue
        exits.setdefault(from_line, set()).add(to_line)
    branch_edges: set[tuple[int, int]] = set()
    for from_line, targets in exits.items():
        if len(targets) <= 1:
            continue
        for to_line in targets:
            branch_edges.add((from_line, to_line))
    return branch_edges


def _call_with_supported_kwargs(func: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return func(*args, **kwargs)
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()):
        return func(*args, **kwargs)
    filtered = {key: value for key, value in kwargs.items() if key in signature.parameters}
    return func(*args, **filtered)


def _module_from_root(module: Any, root: Path) -> bool:
    module_path = getattr(module, "__file__", None)
    if not module_path:
        return False
    try:
        Path(module_path).resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def _clear_modules(prefixes: Iterable[str]) -> None:
    for name in list(sys.modules):
        if any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes):
            sys.modules.pop(name, None)


def _import_module(module_name: str, pyc_root: Path, prefixes: Iterable[str]) -> Any:
    sys.path.insert(0, str(pyc_root))
    try:
        importlib.invalidate_caches()
        _clear_modules(prefixes)
        return importlib.import_module(module_name)
    finally:
        try:
            sys.path.remove(str(pyc_root))
        except ValueError:
            pass


def _loaded_modules_from_root(pyc_root: Path, prefixes: Iterable[str]) -> list[Any]:
    modules: list[Any] = []
    for name, module in list(sys.modules.items()):
        if module is None:
            continue
        if not any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes):
            continue
        if not _module_from_root(module, pyc_root):
            continue
        modules.append(module)
    return modules


def _resolve_pyc_config(
    *,
    target_name: str,
    input_data: bytes,
    parser_config: Mapping[str, Any] | None,
) -> tuple[str, Path, list[str]]:
    if target_name == "cidrize-runner":
        logical = "cidrize"
    elif target_name == "ipv4-parser":
        logical = "ipv4"
    elif target_name == "ipv6-parser":
        logical = "ipv6"
    else:
        logical = _infer_ip_version(input_data)
    ip_version = "ipv4" if logical == "ipv4" else "ipv6"

    module_candidates = DEFAULT_MODULES[logical]
    pyc_root = DEFAULT_PYC_ROOTS.get(
        "cidrize-runner" if logical == "cidrize" else f"{ip_version}-parser"
    )
    if pyc_root is None:
        pyc_root = ROOT_DIR / "blackbox_disam" / "extracted_pyc"

    global_root = None
    entry: Mapping[str, Any] = {}
    if isinstance(parser_config, Mapping):
        raw = parser_config.get("pyc_coverage")
        if isinstance(raw, Mapping):
            raw_root = raw.get("pyc_root")
            if isinstance(raw_root, str) and raw_root.strip():
                global_root = Path(raw_root)
            targets = raw.get("targets")
            if isinstance(targets, Mapping):
                entry = targets.get(target_name, {}) if isinstance(targets.get(target_name), Mapping) else {}

    if entry:
        override_root = entry.get("pyc_root")
        if isinstance(override_root, str) and override_root.strip():
            pyc_root = Path(override_root)
        elif target_name == "IPv4-IPv6-parser":
            ipv_key = "ipv4_root" if ip_version == "ipv4" else "ipv6_root"
            override_root = entry.get(ipv_key)
            if isinstance(override_root, str) and override_root.strip():
                pyc_root = Path(override_root)
    elif global_root is not None:
        rel = DEFAULT_RELATIVE_PYC_SUBDIRS.get(
            "cidrize-runner" if logical == "cidrize" else f"{ip_version}-parser"
        )
        if rel:
            pyc_root = global_root / rel
        else:
            pyc_root = global_root

    if logical == "cidrize":
        override_module = entry.get("module") if isinstance(entry, Mapping) else None
        if isinstance(override_module, str) and override_module.strip():
            module_candidates = [override_module.strip()]
    else:
        override_key = "ipv4_module" if ip_version == "ipv4" else "ipv6_module"
        override_module = entry.get(override_key) if isinstance(entry, Mapping) else None
        if isinstance(override_module, str) and override_module.strip():
            module_candidates = [override_module.strip()]
        elif isinstance(entry.get("module"), str):
            module_candidates = [str(entry.get("module")).strip()]

    return logical, pyc_root, module_candidates


def _prefixes_from_modules(modules: Iterable[str]) -> tuple[str, ...]:
    prefixes = []
    for name in modules:
        prefix = name.split(".", 1)[0]
        if prefix and prefix not in prefixes:
            prefixes.append(prefix)
    return tuple(prefixes) or ("buggy_cidrize", "buggy_ipyparse")


def _resolve_module(
    logical: str, pyc_root: Path, module_candidates: list[str], prefixes: Iterable[str]
) -> Any:
    last_error: Exception | None = None
    for name in module_candidates:
        try:
            return _import_module(name, pyc_root, prefixes)
        except Exception as exc:
            last_error = exc
            continue
    raise RuntimeError(f"Unable to import {module_candidates} from {pyc_root}") from last_error


def _run_cidrize(module: Any, input_str: str) -> None:
    cidrize_func = getattr(module, "cidrize", None)
    if cidrize_func is None:
        raise RuntimeError("cidrize() not found in module")
    _call_with_supported_kwargs(
        cidrize_func,
        input_str,
        strict=False,
        raise_errors=True,
    )


def _run_ipyparse(module: Any, input_str: str, *, logical: str) -> None:
    parser_obj = None
    if logical == "ipv4":
        parser_obj = getattr(module, "IPv4_WholeString", None) or getattr(module, "IPv4", None)
    else:
        parser_obj = getattr(module, "IPv6_WholeString", None) or getattr(module, "IPv6", None)
    if parser_obj is None:
        raise RuntimeError("parser object not found in module")
    _call_with_supported_kwargs(parser_obj.parse_string, input_str, parse_all=True)


class LineCoverageTracer:
    def __init__(self, *, allowed_prefixes: Iterable[str]) -> None:
        self._allowed_prefixes = tuple(allowed_prefixes)
        self._prev_line: dict[int, int] = {}
        self.covered_lines_by_file: dict[str, set[int]] = {}
        self.covered_edges_by_file: dict[str, set[tuple[int, int]]] = {}

    def _should_trace(self, frame: Any) -> bool:
        module_name = str(frame.f_globals.get("__name__", ""))
        return any(
            module_name == prefix or module_name.startswith(prefix + ".")
            for prefix in self._allowed_prefixes
        )

    def _trace(self, frame: Any, event: str, arg: Any) -> Any:
        if event == "call":
            if not self._should_trace(frame):
                return None
            return self._trace
        if not self._should_trace(frame):
            return None
        if event == "line":
            lineno = int(frame.f_lineno or 0)
            if lineno > 0:
                file_name = str(frame.f_code.co_filename or "")
                self.covered_lines_by_file.setdefault(file_name, set()).add(lineno)
                frame_id = id(frame)
                prev_line = self._prev_line.get(frame_id)
                if prev_line is not None and prev_line != lineno:
                    self.covered_edges_by_file.setdefault(file_name, set()).add(
                        (prev_line, lineno)
                    )
                self._prev_line[frame_id] = lineno
        if event in {"return", "exception"}:
            self._prev_line.pop(id(frame), None)
        return self._trace

    def __enter__(self) -> "LineCoverageTracer":
        sys.settrace(self._trace)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        sys.settrace(None)
        return False


def _collect_totals(modules: Iterable[Any]) -> tuple[
    dict[str, set[int]],
    dict[str, set[tuple[int, int]]],
    dict[str, set[tuple[int, int]]],
]:
    total_lines_by_file: dict[str, set[int]] = {}
    total_edges_by_file: dict[str, set[tuple[int, int]]] = {}
    branch_edges_by_file: dict[str, set[tuple[int, int]]] = {}

    for module in modules:
        loader = getattr(module, "__loader__", None)
        if loader is None:
            spec = getattr(module, "__spec__", None)
            loader = getattr(spec, "loader", None) if spec is not None else None
        get_code = getattr(loader, "get_code", None) if loader is not None else None
        if get_code is None:
            continue
        try:
            top_code = get_code(module.__name__)
        except Exception:
            continue
        if not isinstance(top_code, types.CodeType):
            continue
        for code_obj in _iter_code_objects(top_code):
            file_name = str(code_obj.co_filename or "")
            if not file_name:
                continue
            total_lines_by_file.setdefault(file_name, set()).update(
                _collect_line_numbers(code_obj)
            )
            offset_edges = _build_offset_edges(code_obj)
            line_edges = _offset_edges_to_line_edges(
                offset_edges, _line_map_for_code(code_obj)
            )
            if line_edges:
                total_edges_by_file.setdefault(file_name, set()).update(line_edges)

    for file_name, edges in total_edges_by_file.items():
        branch_edges_by_file[file_name] = _branch_edge_set(edges)

    return total_lines_by_file, total_edges_by_file, branch_edges_by_file


def run_pyc_coverage(
    *,
    target_name: str,
    input_data: bytes,
    parser_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    logical, pyc_root, module_candidates = _resolve_pyc_config(
        target_name=target_name,
        input_data=input_data,
        parser_config=parser_config,
    )
    prefixes = _prefixes_from_modules(module_candidates)
    if not pyc_root.is_dir():
        return {
            "status": "error",
            "bug_signature": None,
            "error": f"pyc root not found: {pyc_root}",
            "covered_branches": None,
            "missing_branches": None,
            "branch_details_by_file": [],
            "coverage_backend": "pyc-line",
            "covered_lines": 0,
            "total_lines": 0,
            "covered_edges": 0,
            "total_edges": 0,
            "total_branches": 0,
        }

    try:
        module = _resolve_module(logical, pyc_root, module_candidates, prefixes)
    except Exception as exc:
        return {
            "status": "error",
            "bug_signature": None,
            "error": str(exc),
            "covered_branches": None,
            "missing_branches": None,
            "branch_details_by_file": [],
            "coverage_backend": "pyc-line",
            "covered_lines": 0,
            "total_lines": 0,
            "covered_edges": 0,
            "total_edges": 0,
            "total_branches": 0,
        }

    input_str = input_data.decode("utf-8", errors="replace").strip()
    tracer = LineCoverageTracer(allowed_prefixes=prefixes)
    status = "ok"
    bug_signature = None
    with tracer:
        try:
            if logical == "cidrize":
                _run_cidrize(module, input_str)
            else:
                _run_ipyparse(module, input_str, logical=logical)
        except Exception as exc:
            status = "bug"
            bug_signature = {
                "type": type(exc).__name__,
                "exception": type(exc).__name__,
                "message": str(exc),
                "file": getattr(exc, "file", None),
                "line": getattr(exc, "line", None),
            }

    modules = _loaded_modules_from_root(pyc_root, prefixes)
    total_lines_by_file, total_edges_by_file, branch_edges_by_file = _collect_totals(
        modules
    )

    covered_lines_total = sum(
        len(lines) for lines in tracer.covered_lines_by_file.values()
    )
    covered_edges_total = sum(
        len(edges) for edges in tracer.covered_edges_by_file.values()
    )

    covered_branches_total = 0
    for file_name, edges in tracer.covered_edges_by_file.items():
        covered_branches_total += len(edges & branch_edges_by_file.get(file_name, set()))

    total_lines = sum(len(lines) for lines in total_lines_by_file.values())
    total_edges = sum(len(edges) for edges in total_edges_by_file.values())
    total_branches = sum(len(edges) for edges in branch_edges_by_file.values())
    missing_branches = max(0, total_branches - covered_branches_total)

    branch_details_by_file: list[dict[str, Any]] = []
    for file_name, edges in sorted(tracer.covered_edges_by_file.items()):
        if not edges:
            continue
        branch_details_by_file.append(
            {
                "file": file_name,
                "covered_branches": [
                    {"from_line": int(from_line), "to_line": int(to_line)}
                    for from_line, to_line in sorted(edges)
                    if from_line > 0 and to_line > 0
                ],
                "missing_branches": [],
            }
        )

    return {
        "status": status,
        "bug_signature": bug_signature,
        "covered_branches": covered_branches_total,
        "missing_branches": missing_branches,
        "branch_details_by_file": branch_details_by_file,
        "coverage_backend": "pyc-line",
        "covered_lines": covered_lines_total,
        "total_lines": total_lines,
        "covered_edges": covered_edges_total,
        "total_edges": total_edges,
        "total_branches": total_branches,
    }


__all__ = ["pyc_coverage_supported", "run_pyc_coverage"]
