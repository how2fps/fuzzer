from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any


WHITESPACE_RE = re.compile(r"\s+")
TIMESTAMP_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}[\sT]\d{2}:\d{2}:\d{2}[.\d]*Z?\b", re.I)
HEX_RE = re.compile(r"0x[0-9a-fA-F]+")
LONG_NUM_RE = re.compile(r"\b\d{6,}\b")
TOKEN_RE = re.compile(r"[A-Za-z]+|\d+|\s+|[^A-Za-z0-9\s]+")
PARSE_OFFSET_PATTERNS = (
    re.compile(r"\bchar\s+(\d+)\b", re.I),
    re.compile(r"\bcolumn\s+(\d+)\b", re.I),
    re.compile(r"\bcol(?:umn)?[:=]?\s*(\d+)\b", re.I),
    re.compile(r"\bposition\s+(\d+)\b", re.I),
    re.compile(r"\bat\s+char\s+(\d+)\b", re.I),
)
_UNSET = object()


def _stable_hash(payload: Any) -> str:
    raw = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def _normalize_text_for_signature(text: str) -> str:
    normalized = TIMESTAMP_RE.sub("<TIMESTAMP>", text)
    normalized = HEX_RE.sub("<HEX>", normalized)
    normalized = LONG_NUM_RE.sub("<NUM>", normalized)
    normalized = WHITESPACE_RE.sub(" ", normalized).strip()
    return normalized


def text_signature(text: str | None, *, prefix: str) -> str | None:
    if text is None:
        return None
    normalized = _normalize_text_for_signature(str(text))
    if not normalized:
        return None
    return f"{prefix}:{_stable_hash(normalized)}"


def _to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, Mapping):
        return {
            str(key): _to_jsonable(sub_value)
            for key, sub_value in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_to_jsonable(item) for item in value]
    return str(value)


def semantic_output_signature(value: Any) -> str | None:
    if value is None:
        return None
    return "SEM:" + _stable_hash(_to_jsonable(value))


def semantic_output_category(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, tuple):
        return "tuple"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    return type(value).__name__.lower()


def execution_time_bucket(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return ""
    if seconds < 0.005:
        return "sub_5ms"
    if seconds < 0.02:
        return "5_20ms"
    if seconds < 0.1:
        return "20_100ms"
    if seconds < 0.5:
        return "100_500ms"
    if seconds < 2.0:
        return "500ms_2s"
    return "2s_plus"


def extract_semantic_output(result: Mapping[str, Any] | None) -> Any:
    if not isinstance(result, Mapping):
        return None
    if "semantic_output" in result:
        return result.get("semantic_output")
    for key in ("decoded", "parsed"):
        if key in result:
            return result.get(key)
    return None


def error_code_from_result(
    result: Mapping[str, Any] | None,
    *,
    returncode: int | None = None,
) -> str:
    if not isinstance(result, Mapping):
        return ""
    bug_signature = result.get("bug_signature")
    if isinstance(bug_signature, Mapping):
        exception = str(bug_signature.get("exception") or "").strip()
        bug_type = str(bug_signature.get("type") or "").strip()
        if exception:
            return exception
        if bug_type:
            return bug_type
    if returncode not in (None, 0):
        return f"exit:{returncode}"
    status = str(result.get("status") or "").strip().lower()
    if status in {"bug", "crash", "timeout", "error"}:
        return status
    return ""


def enrich_execution_result(
    result: dict[str, Any],
    *,
    stdout: str | None = None,
    stderr: str | None = None,
    semantic_output: Any = _UNSET,
    execution_time_seconds: float | None = None,
    returncode: int | None = None,
) -> dict[str, Any]:
    if stdout is not None and "stdout_signature" not in result:
        result["stdout_signature"] = text_signature(stdout, prefix="STDOUT")
    if stderr is not None and "stderr_signature" not in result:
        result["stderr_signature"] = text_signature(stderr, prefix="STDERR")

    if semantic_output is _UNSET:
        resolved_semantic_output = extract_semantic_output(result)
    else:
        resolved_semantic_output = semantic_output
    if resolved_semantic_output is not None:
        result["semantic_output"] = _to_jsonable(resolved_semantic_output)
        result["semantic_output_signature"] = semantic_output_signature(
            resolved_semantic_output
        )
    elif "semantic_output_signature" not in result:
        result["semantic_output_signature"] = None

    status = str(result.get("status") or "").strip().lower()
    parse_category = semantic_output_category(extract_semantic_output(result))
    if parse_category == "none" and status not in {"", "ok"}:
        error_code = error_code_from_result(result, returncode=returncode)
        parse_category = f"error:{error_code or status}"
    result["parse_category"] = parse_category
    result["error_code"] = error_code_from_result(result, returncode=returncode)

    if execution_time_seconds is not None:
        result["execution_time_seconds"] = max(0.0, float(execution_time_seconds))
        result["execution_time_bucket"] = execution_time_bucket(execution_time_seconds)
    elif "execution_time_bucket" not in result:
        raw_seconds = result.get("execution_time_seconds")
        try:
            result["execution_time_bucket"] = execution_time_bucket(float(raw_seconds))
        except (TypeError, ValueError):
            result["execution_time_bucket"] = ""

    return result


def result_behavior_summary(result: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        return {
            "status": "",
            "success": False,
            "parse_category": "",
            "output_signature": "",
            "output_class": "none",
            "error_code": "",
            "execution_time_bucket": "",
        }
    status = str(result.get("status") or "").strip().lower()
    semantic_output = extract_semantic_output(result)
    output_signature = result.get("semantic_output_signature")
    if output_signature in (None, ""):
        output_signature = result.get("stdout_signature")
    if semantic_output is not None:
        output_class = "semantic:" + semantic_output_category(semantic_output)
    elif result.get("stdout_signature") not in (None, ""):
        output_class = "text"
    elif result.get("stderr_signature") not in (None, ""):
        output_class = "stderr"
    else:
        output_class = "none"
    return {
        "status": status,
        "success": status == "ok",
        "parse_category": str(result.get("parse_category") or ""),
        "output_signature": str(output_signature or ""),
        "output_class": output_class,
        "error_code": str(result.get("error_code") or ""),
        "execution_time_bucket": str(result.get("execution_time_bucket") or ""),
    }


def build_differential_behavior(
    *,
    closed_result: Mapping[str, Any] | None,
    open_result: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(open_result, Mapping) or not open_result:
        return None

    closed = result_behavior_summary(closed_result)
    open_ = result_behavior_summary(open_result)
    mismatches: dict[str, dict[str, Any]] = {}

    fields = (
        ("success_failure", "success"),
        ("parse_category", "parse_category"),
        ("normalized_output", "output_signature"),
        ("error_code", "error_code"),
        ("execution_time_bucket", "execution_time_bucket"),
    )
    for label, field in fields:
        if closed[field] == open_[field]:
            continue
        mismatches[label] = {
            "closed": closed[field],
            "open": open_[field],
        }

    if not mismatches:
        return None

    mismatch_types = tuple(sorted(mismatches))
    payload = {
        "mismatch_types": mismatch_types,
        "mismatches": mismatches,
        "closed_status": closed["status"],
        "open_status": open_["status"],
    }
    pattern_payload = {
        "mismatch_types": mismatch_types,
        "closed_status": closed["status"],
        "open_status": open_["status"],
        "closed_parse_category": closed["parse_category"],
        "open_parse_category": open_["parse_category"],
        "closed_output_class": closed["output_class"],
        "open_output_class": open_["output_class"],
        "closed_error_code": closed["error_code"],
        "open_error_code": open_["error_code"],
        "closed_execution_time_bucket": closed["execution_time_bucket"],
        "open_execution_time_bucket": open_["execution_time_bucket"],
    }
    return {
        "mismatch_types": mismatch_types,
        "mismatches": mismatches,
        "mismatch_type_key": "+".join(mismatch_types),
        "pattern_key": "DIFFPAT:" + _stable_hash(pattern_payload),
        "behavior_key": "DIFF:" + _stable_hash(payload),
    }


def token_structure_signature(text: str) -> str:
    if not text:
        return "empty"
    classes: list[str] = []
    for token in TOKEN_RE.findall(text):
        if token.isspace():
            classes.append("ws")
        elif token.isalpha():
            classes.append(f"alpha:{min(len(token), 8)}")
        elif token.isdigit():
            classes.append(f"num:{min(len(token), 8)}")
        else:
            classes.append(f"sym:{token[:4]}")
    trimmed = classes[:24]
    if len(classes) > len(trimmed):
        trimmed.append("...")
    return "TOK:" + _stable_hash(trimmed)


def length_bucket(text: str) -> str:
    length = len(text)
    if length == 0:
        return "0"
    upper = 1 << int(math.ceil(math.log2(max(1, length))))
    lower = max(0, (upper // 2) + (1 if upper > 1 else 0))
    if upper <= 1:
        return "1"
    if lower <= 1:
        return f"1-{upper}"
    return f"{lower}-{upper}"


def describe_input_structure(text: str) -> dict[str, Any]:
    return {
        "token_structure_key": token_structure_signature(text),
        "length_bucket": length_bucket(text),
        "token_count": len(TOKEN_RE.findall(text)),
        "character_count": len(text),
    }


def _parse_offsets_from_message(message: str) -> list[int]:
    offsets: list[int] = []
    for pattern in PARSE_OFFSET_PATTERNS:
        for match in pattern.finditer(message):
            try:
                offsets.append(int(match.group(1)))
            except (IndexError, TypeError, ValueError):
                continue
    return offsets


def late_parse_depth_from_result(
    *,
    result: Mapping[str, Any] | None,
    input_text: str,
) -> float:
    if not isinstance(result, Mapping) or not input_text:
        return 0.0

    depths: list[float] = []
    for key in ("closed_result", "open_result", "shadow_result"):
        nested = result.get(key)
        if not isinstance(nested, Mapping):
            continue
        bug_signature = nested.get("bug_signature")
        if not isinstance(bug_signature, Mapping):
            continue
        message = str(bug_signature.get("message") or "")
        offsets = _parse_offsets_from_message(message)
        if not offsets:
            continue
        max_offset = max(0, max(offsets))
        depths.append(min(1.0, max_offset / float(max(1, len(input_text)))))
    return max(depths, default=0.0)


def partial_parse_success(
    *,
    result: Mapping[str, Any] | None,
    input_text: str,
) -> float:
    late_depth = late_parse_depth_from_result(result=result, input_text=input_text)
    if late_depth >= 0.9:
        return 1.0
    if late_depth >= 0.65:
        return 0.5
    return 0.0


def execution_stability_bonus(
    *,
    closed_result: Mapping[str, Any] | None,
    open_result: Mapping[str, Any] | None,
) -> float:
    if not isinstance(closed_result, Mapping) or not isinstance(open_result, Mapping):
        return 0.0

    try:
        closed_seconds = float(closed_result.get("execution_time_seconds"))
        open_seconds = float(open_result.get("execution_time_seconds"))
    except (TypeError, ValueError):
        return 0.0

    if closed_seconds <= 0.0 or open_seconds <= 0.0:
        return 0.0

    ratio = max(closed_seconds, open_seconds) / max(min(closed_seconds, open_seconds), 1e-9)
    closed_bucket = str(closed_result.get("execution_time_bucket") or "")
    open_bucket = str(open_result.get("execution_time_bucket") or "")
    if closed_bucket and closed_bucket == open_bucket and ratio <= 1.15:
        return 1.0
    if closed_bucket and closed_bucket == open_bucket and ratio <= 1.5:
        return 0.5
    return 0.0
