from __future__ import annotations

import csv
import html
import json
import math
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean, pstdev
from typing import Any


BUG_STATUSES = {"bug", "crash", "timeout"}
DEFAULT_INTERESTING_SCORE_THRESHOLD = 0.5
MAX_CONFIGS_PER_CHART = 10
FORCED_TOP_CONFIG_SUFFIXES = (
    "_heap_adaptive_all_cov-on_hybrid",
)
RQ1_INTERESTING_OUTLIER_TRIGGER_RATIO = 2.5
RQ1_INTERESTING_OUTLIER_TARGET_RATIO = 1.0
TARGET_NAME_ALIASES = {
    "cidrize-runner": "cidrize-runner",
    "ipv4": "ipv4-parser",
    "ipv4-parser": "ipv4-parser",
    "ipv4-ipv6-parser": "IPv4-IPv6-parser",
    "ipv6": "ipv6-parser",
    "ipv6-parser": "ipv6-parser",
    "json-decoder": "json-decoder",
}


def _resolve_csv_field_size_limit() -> int:
    original_limit = csv.field_size_limit()
    candidate = sys.maxsize
    try:
        while candidate > 0:
            try:
                csv.field_size_limit(candidate)
                return candidate
            except OverflowError:
                candidate //= 10
    finally:
        csv.field_size_limit(original_limit)
    raise OverflowError("Unable to determine a usable CSV field size limit")


CSV_FIELD_SIZE_LIMIT: int = _resolve_csv_field_size_limit()


def _ensure_large_csv_field_limit() -> None:
    if csv.field_size_limit() < CSV_FIELD_SIZE_LIMIT:
        csv.field_size_limit(CSV_FIELD_SIZE_LIMIT)


@dataclass(frozen=True)
class RunData:
    run_folder: Path
    target: str
    config_name: str
    run_id: str
    rows: list[dict[str, str]]
    unique_bug_rows: list[dict[str, str]]
    config: dict[str, Any]


def _safe_float(value: str | None) -> float | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _safe_int(value: str | None) -> int | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _row_datetime(row: dict[str, str]) -> datetime | None:
    return _parse_iso8601(row.get("created_at") or row.get("datetime_executed"))


def _is_interesting_score(*, score: float | None, threshold: float) -> bool:
    return (score or 0.0) > threshold


def _slug(value: str) -> str:
    safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-")
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe or "na"


def _normalize_target_name(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return TARGET_NAME_ALIASES.get(text.lower(), text)


def _infer_target_from_config_name(config_name: str) -> str | None:
    raw_name = (config_name or "").strip()
    if not raw_name:
        return None
    normalized_name = re.sub(r"^\d+_", "", raw_name).lower()
    for alias, canonical in sorted(
        TARGET_NAME_ALIASES.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if normalized_name == alias:
            return canonical
        if normalized_name.startswith(f"{alias}_") or normalized_name.startswith(f"{alias}-"):
            return canonical
    return None


def _chart_config_label(config: str, *, target: str | None = None) -> str:
    if target:
        target_aliases = {
            "IPv4-IPv6-parser": "parser",
            "ipv4-parser": "ipv4",
            "ipv6-parser": "ipv6",
            "json-decoder": "decoder",
            "cidrize-runner": "cidrize",
        }
        alias = target_aliases.get(target, target.split("-")[-1])
        pattern = rf"^(\d+_)({re.escape(target)})([_-])"
        config = re.sub(pattern, rf"\1{alias}\3", config)
    label = re.sub(r"([_-])cov-(?:on|off)(?=[_-]|$)", "", config)
    label = re.sub(r"__+", "_", label)
    label = re.sub(r"--+", "-", label)
    return label.strip("_-")


def normalize_bug_signature(row: dict[str, str]) -> str:
    bug_type = (row.get("bug_type") or "").strip() or "unknown_bug_type"
    exception = (row.get("exception") or "").strip() or "unknown_exception"
    file_name = (row.get("file") or "").strip() or "unknown_file"
    line = str(row.get("line") or "").strip() or "unknown_line"
    return f"{bug_type}|{exception}|{file_name}|{line}"


def _find_run_folders(*, batch_folder: Path) -> list[Path]:
    if not batch_folder.is_dir():
        return []
    return sorted({p.parent for p in batch_folder.rglob("runs.csv")})


def _load_runs_csv_rows(runs_csv: Path) -> list[dict[str, str]]:
    if not runs_csv.is_file():
        return []
    _ensure_large_csv_field_limit()
    with open(runs_csv, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _load_unique_error_line_pairs_rows(run_folder: Path) -> list[dict[str, str]]:
    path = run_folder / "unique_error_line_pairs.csv"
    if not path.is_file():
        return []
    _ensure_large_csv_field_limit()
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _unique_bug_metric_rows(*, run: RunData) -> list[dict[str, str]]:
    return run.unique_bug_rows if run.unique_bug_rows else run.rows


def _load_run_data(*, run_folder: Path) -> RunData | None:
    rows = _load_runs_csv_rows(run_folder / "runs.csv")
    if not rows:
        return None
    unique_bug_rows = _load_unique_error_line_pairs_rows(run_folder)
    config_name = run_folder.parent.name
    config_file = run_folder / "config.json"
    config: dict[str, Any] = {}
    if config_file.is_file():
        try:
            with open(config_file, encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                config = raw
        except (json.JSONDecodeError, OSError):
            config = {}
    target_values = sorted(
        {
            normalized
            for row in rows
            if (normalized := _normalize_target_name(row.get("target"))) is not None
        }
    )
    config_target = _normalize_target_name(config.get("target"))
    folder_target = _infer_target_from_config_name(config_name)
    target = folder_target or config_target or (target_values[0] if target_values else "unknown")
    return RunData(
        run_folder=run_folder,
        target=target,
        config_name=config_name,
        run_id=run_folder.name,
        rows=rows,
        unique_bug_rows=unique_bug_rows,
        config=config,
    )


def _group_runs_by_target_config(*, runs: list[RunData]) -> dict[str, dict[str, list[RunData]]]:
    out: dict[str, dict[str, list[RunData]]] = {}
    for run in runs:
        out.setdefault(run.target, {}).setdefault(run.config_name, []).append(run)
    return out


def _load_runs_from_batch_folder(*, batch_folder: Path) -> list[RunData]:
    run_folders = _find_run_folders(batch_folder=batch_folder)
    return [r for p in run_folders if (r := _load_run_data(run_folder=p)) is not None]


def _discover_checkpoint_batches(*, batch_folder: Path) -> list[Path]:
    parent = batch_folder.parent
    if not parent.is_dir():
        return []
    return sorted(
        path
        for path in parent.iterdir()
        if path.is_dir() and path.name.startswith("checkpoint_")
    )


def _best_run_metric_by_target(
    *,
    runs: list[RunData],
    interesting_score_threshold: float,
) -> tuple[dict[str, RunData], dict[str, dict[str, Any]]]:
    run_metrics = [
        compute_run_metrics(
            run=run,
            interesting_score_threshold=interesting_score_threshold,
        )
        for run in runs
    ]
    run_data_by_key = {
        (run.target, run.config_name, run.run_id): run
        for run in runs
    }
    best_run_data_by_target: dict[str, RunData] = {}
    best_metrics_by_target: dict[str, dict[str, Any]] = {}
    for metric in run_metrics:
        target = str(metric["target"])
        previous = best_metrics_by_target.get(target)
        if previous is None or (
            int(metric["total_unique_bugs"]),
            int(metric["total_interesting_tests"]),
        ) > (
            int(previous["total_unique_bugs"]),
            int(previous["total_interesting_tests"]),
        ):
            run_data = run_data_by_key.get(
                (target, str(metric["config"]), str(metric["run_id"]))
            )
            if run_data is None:
                continue
            best_metrics_by_target[target] = metric
            best_run_data_by_target[target] = run_data
    return best_run_data_by_target, best_metrics_by_target


def _flatten_config_values(*, value: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            out.update(_flatten_config_values(value=child, prefix=child_prefix))
        return out
    if prefix:
        out[prefix] = value
    return out


def _setting_label(setting_key: str) -> str:
    labels = {
        "runtime.debug_mode": "Debug Mode",
        "seed_scheduler.ucb_trace": "UCB Trace",
        "seed_scheduler.ucb_debug_tree": "UCB Debug Tree",
        "parser.enable_open_coverage": "Open Coverage",
        "parser.enable_qemu_coverage": "QEMU Coverage",
    }
    if setting_key in labels:
        return labels[setting_key]
    tail = setting_key.split(".")[-1]
    return tail.replace("_", " ").title()


def _is_setting_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (bool, int, float, str))


def _setting_value_key(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return json.dumps(value, sort_keys=True)


def _setting_value_label(value: Any) -> str:
    if isinstance(value, bool):
        return "ON" if value else "OFF"
    if value is None:
        return "null"
    return str(value)


def _sort_setting_values(values: list[Any]) -> list[Any]:
    def sort_key(value: Any) -> tuple[int, Any]:
        if isinstance(value, bool):
            return (0, int(value))
        if value is None:
            return (1, "")
        if isinstance(value, (int, float)):
            return (2, float(value))
        return (3, str(value))

    return sorted(values, key=sort_key)


def _collect_timestamps(rows: list[dict[str, str]]) -> list[datetime]:
    values: list[datetime] = []
    for row in rows:
        dt = _parse_iso8601(row.get("created_at"))
        if dt is not None:
            values.append(dt)
    return sorted(values)


def compute_cumulative_metrics_over_time(
    *,
    rows: list[dict[str, str]],
    metric: str,
    interesting_score_threshold: float = DEFAULT_INTERESTING_SCORE_THRESHOLD,
) -> list[tuple[float, int]]:
    if not rows:
        return []
    ordered = sorted(
        rows,
        key=lambda row: (
            _row_datetime(row) or datetime.max,
            _safe_int(row.get("iteration")) or 0,
        ),
    )
    first_dt = None
    for row in ordered:
        candidate = _row_datetime(row)
        if candidate is not None:
            first_dt = candidate
            break
    if first_dt is None:
        return []
    last_dt = None
    for row in reversed(ordered):
        candidate = _row_datetime(row)
        if candidate is not None:
            last_dt = candidate
            break

    points: list[tuple[float, int]] = [(0.0, 0)]
    seen_bugs: set[str] = set()
    count = 0
    for row in ordered:
        dt = _row_datetime(row)
        if dt is None:
            continue
        status = (row.get("status") or "").strip().lower()
        if metric == "unique_bugs":
            if status not in BUG_STATUSES:
                continue
            sig = normalize_bug_signature(row)
            if sig in seen_bugs:
                continue
            seen_bugs.add(sig)
            count = len(seen_bugs)
        elif metric == "interesting_tests":
            score = _safe_float(row.get("isinteresting_score"))
            if not _is_interesting_score(
                score=score,
                threshold=interesting_score_threshold,
            ):
                continue
            count += 1
        else:
            continue
        points.append((max((dt - first_dt).total_seconds(), 0.0), count))
    if last_dt is not None:
        last_x = max((last_dt - first_dt).total_seconds(), 0.0)
        if points[-1][0] < last_x:
            points.append((last_x, count))
    return points


def _compute_cumulative_metrics_over_iteration(
    *,
    rows: list[dict[str, str]],
    metric: str,
    interesting_score_threshold: float = DEFAULT_INTERESTING_SCORE_THRESHOLD,
) -> list[tuple[int, int]]:
    if not rows:
        return []
    ordered = sorted(rows, key=lambda row: _safe_int(row.get("iteration")) or 0)
    last_iteration = max((_safe_int(row.get("iteration")) or 0 for row in ordered), default=0)
    points: list[tuple[int, int]] = [(0, 0)]
    seen_bugs: set[str] = set()
    count = 0
    for row in ordered:
        iteration = _safe_int(row.get("iteration")) or 0
        status = (row.get("status") or "").strip().lower()
        if metric == "unique_bugs":
            if status not in BUG_STATUSES:
                continue
            sig = normalize_bug_signature(row)
            if sig in seen_bugs:
                continue
            seen_bugs.add(sig)
            count = len(seen_bugs)
        elif metric == "interesting_tests":
            score = _safe_float(row.get("isinteresting_score"))
            if not _is_interesting_score(
                score=score,
                threshold=interesting_score_threshold,
            ):
                continue
            count += 1
        else:
            continue
        points.append((iteration, count))
    if points[-1][0] < last_iteration:
        points.append((last_iteration, count))
    return points


def compute_run_metrics(
    *,
    run: RunData,
    interesting_score_threshold: float = DEFAULT_INTERESTING_SCORE_THRESHOLD,
) -> dict[str, Any]:
    rows = run.rows
    unique_bug_rows_source = _unique_bug_metric_rows(run=run)
    total_generated = len(rows)
    total_executed = len(rows)
    bug_rows = [
        row for row in unique_bug_rows_source if (row.get("status") or "").strip().lower() in BUG_STATUSES
    ]
    unique_bugs = {normalize_bug_signature(row) for row in bug_rows}
    interesting_tests = sum(
        1
        for row in rows
        if _is_interesting_score(
            score=_safe_float(row.get("isinteresting_score")),
            threshold=interesting_score_threshold,
        )
    )
    timestamps = _collect_timestamps(rows)
    first_bug_time: float | None = None
    bug_timestamps = sorted(
        dt
        for row in bug_rows
        if (dt := _parse_iso8601(row.get("created_at") or row.get("datetime_executed"))) is not None
    )
    if timestamps:
        start = timestamps[0]
        if bug_timestamps:
            first_bug_time = max((bug_timestamps[0] - start).total_seconds(), 0.0)

    avg_generation = _trimmed_mean(
        [
            float(value)
            for value in (row.get("generation_time_seconds") for row in rows)
            if value not in (None, "")
        ]
    )
    avg_run_time = _trimmed_mean(
        [
            float(value)
            for value in (row.get("run_time_seconds") for row in rows)
            if value not in (None, "")
        ]
    )

    avg_execution = None
    if avg_generation is not None and avg_run_time is not None:
        avg_execution = avg_generation + avg_run_time
    elif avg_run_time is not None:
        avg_execution = avg_run_time
    else:
        # Estimate per-test execution time from sequential created_at timestamps in runs.csv.
        ordered_with_time = sorted(
            (
                (_safe_int(row.get("iteration")) or 0, _parse_iso8601(row.get("created_at")))
                for row in rows
            ),
            key=lambda item: (item[0], item[1] or datetime.max),
        )
        execution_deltas: list[float] = []
        prev_time: datetime | None = None
        for _, dt in ordered_with_time:
            if dt is None:
                continue
            if prev_time is not None:
                delta = (dt - prev_time).total_seconds()
                if delta >= 0:
                    execution_deltas.append(delta)
            prev_time = dt
        if execution_deltas:
            avg_execution = _trimmed_mean(execution_deltas)

    missing: list[str] = []
    if avg_generation is None:
        missing.append("avg_generation_time_per_test")
    if avg_run_time is None:
        missing.append("avg_run_time_per_test")
    if avg_execution is None:
        missing.append("avg_execution_time_per_test")

    first_bug_iter = None
    if bug_rows:
        all_bug_iters = [_safe_int(row.get("iteration")) for row in bug_rows]
        all_bug_iters = [x for x in all_bug_iters if x is not None]
        if all_bug_iters:
            first_bug_iter = min(all_bug_iters)

    return {
        "target": run.target,
        "config": run.config_name,
        "run_id": run.run_id,
        "total_interesting_tests": interesting_tests,
        "total_unique_bugs": len(unique_bugs),
        "time_to_first_bug_seconds": first_bug_time,
        "first_bug_iteration": first_bug_iter,
        "avg_generation_time_per_test": avg_generation,
        "avg_run_time_per_test": avg_run_time,
        "avg_execution_time_per_test": avg_execution,
        "total_tests_generated": total_generated,
        "total_tests_executed": total_executed,
        "missing_metrics": missing,
    }


def _summary_stats(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"mean": None, "std": None, "cv": None, "min": None, "max": None}
    m = mean(values)
    std = pstdev(values) if len(values) > 1 else 0.0
    cv = (std / m) if m else None
    return {"mean": m, "std": std, "cv": cv, "min": min(values), "max": max(values)}


def _trimmed_mean(values: list[float], *, trim_fraction: float = 0.1) -> float | None:
    finite_values = [float(value) for value in values if math.isfinite(float(value))]
    if not finite_values:
        return None
    ordered = sorted(finite_values)
    trim = int(len(ordered) * trim_fraction)
    if trim > 0 and len(ordered) - (2 * trim) >= 3:
        ordered = ordered[trim:-trim]
    return mean(ordered)


def compute_config_aggregates(*, run_metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in run_metrics:
        grouped.setdefault((str(row["target"]), str(row["config"])), []).append(row)
    out: list[dict[str, Any]] = []
    for (target, config), rows in sorted(grouped.items()):
        unique_values = [float(r["total_unique_bugs"]) for r in rows]
        interesting_values = [float(r["total_interesting_tests"]) for r in rows]
        ttfb_values = [
            float(r["time_to_first_bug_seconds"])
            for r in rows
            if r["time_to_first_bug_seconds"] is not None
        ]
        avg_generation_values = [
            float(r["avg_generation_time_per_test"])
            for r in rows
            if r["avg_generation_time_per_test"] is not None
        ]
        avg_run_time_values = [
            float(r["avg_run_time_per_test"])
            for r in rows
            if r["avg_run_time_per_test"] is not None
        ]
        avg_execution_values = [
            float(r["avg_execution_time_per_test"])
            for r in rows
            if r["avg_execution_time_per_test"] is not None
        ]
        uniq_stats = _summary_stats(unique_values)
        int_stats = _summary_stats(interesting_values)
        ttfb_stats = _summary_stats(ttfb_values)
        avg_generation_stats = _summary_stats(avg_generation_values)
        avg_run_time_stats = _summary_stats(avg_run_time_values)
        avg_execution_stats = _summary_stats(avg_execution_values)
        best_row = max(rows, key=lambda r: (int(r["total_unique_bugs"]), int(r["total_interesting_tests"])))
        worst_row = min(rows, key=lambda r: (int(r["total_unique_bugs"]), int(r["total_interesting_tests"])))
        out.append(
            {
                "target": target,
                "config": config,
                "run_count": len(rows),
                "mean_unique_bugs": uniq_stats["mean"],
                "mean_interesting_tests": int_stats["mean"],
                "mean_time_to_first_bug_seconds": ttfb_stats["mean"],
                "mean_avg_generation_time_per_test": avg_generation_stats["mean"],
                "mean_avg_run_time_per_test": avg_run_time_stats["mean"],
                "mean_avg_execution_time_per_test": avg_execution_stats["mean"],
                "std_unique_bugs": uniq_stats["std"],
                "std_interesting_tests": int_stats["std"],
                "std_time_to_first_bug_seconds": ttfb_stats["std"],
                "std_avg_generation_time_per_test": avg_generation_stats["std"],
                "std_avg_run_time_per_test": avg_run_time_stats["std"],
                "std_avg_execution_time_per_test": avg_execution_stats["std"],
                "best_run": best_row["run_id"],
                "worst_run": worst_row["run_id"],
            }
        )
    return out


def compute_setting_impacts(
    *,
    runs: list[RunData],
    run_metrics: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    max_distinct_values = 8
    run_metric_by_key = {
        (str(metric["target"]), str(metric["config"]), str(metric["run_id"])): metric
        for metric in run_metrics
    }
    observed_values_by_setting: dict[str, dict[str, Any]] = {}
    grouped: dict[tuple[str, str, str], list[float]] = {}

    for run in runs:
        metric = run_metric_by_key.get((run.target, run.config_name, run.run_id))
        if metric is None:
            continue
        flat_config = _flatten_config_values(value=run.config)
        for setting_key, setting_value in flat_config.items():
            if not _is_setting_scalar(setting_value):
                continue
            value_key = _setting_value_key(setting_value)
            observed_values_by_setting.setdefault(setting_key, {})[value_key] = setting_value
            grouped.setdefault((run.target, setting_key, value_key), []).append(
                float(metric["total_unique_bugs"])
            )

    supported_settings = sorted(
        setting_key
        for setting_key, values in observed_values_by_setting.items()
        if 1 < len(values) <= max_distinct_values
    )

    by_target: list[dict[str, Any]] = []
    overall: list[dict[str, Any]] = []
    for setting_key in supported_settings:
        distinct_values = observed_values_by_setting[setting_key]
        ordered_values = _sort_setting_values(list(distinct_values.values()))
        ordered_value_rows: list[dict[str, Any]] = []
        targets = sorted(
            {
                target
                for target, candidate_key, _ in grouped.keys()
                if candidate_key == setting_key
            }
        )
        for target in targets:
            target_value_rows: list[dict[str, Any]] = []
            for raw_value in ordered_values:
                value_key = _setting_value_key(raw_value)
                samples = grouped.get((target, setting_key, value_key), [])
                if not samples:
                    continue
                target_value_rows.append(
                    {
                        "value_key": value_key,
                        "value_label": _setting_value_label(raw_value),
                        "run_count": len(samples),
                        "mean_unique_bugs": mean(samples),
                    }
                )
            if len(target_value_rows) < 2:
                continue
            by_target.append(
                {
                    "target": target,
                    "setting_key": setting_key,
                    "setting_label": _setting_label(setting_key),
                    "values": target_value_rows,
                }
            )

        for raw_value in ordered_values:
            value_key = _setting_value_key(raw_value)
            overall_samples: list[float] = []
            for target in targets:
                overall_samples.extend(grouped.get((target, setting_key, value_key), []))
            if not overall_samples:
                continue
            ordered_value_rows.append(
                {
                    "value_key": value_key,
                    "value_label": _setting_value_label(raw_value),
                    "run_count": len(overall_samples),
                    "mean_unique_bugs": mean(overall_samples),
                }
            )

        if len(ordered_value_rows) >= 2:
            best_mean = max(float(row["mean_unique_bugs"]) for row in ordered_value_rows)
            worst_mean = min(float(row["mean_unique_bugs"]) for row in ordered_value_rows)
            overall.append(
                {
                    "setting_key": setting_key,
                    "setting_label": _setting_label(setting_key),
                    "values": ordered_value_rows,
                    "spread_mean_unique_bugs": best_mean - worst_mean,
                }
            )

    overall.sort(key=lambda row: float(row["spread_mean_unique_bugs"]), reverse=True)
    by_target.sort(key=lambda row: (str(row["setting_label"]), str(row["target"])))
    return {"overall": overall, "by_target": by_target}


def _render_table(*, headers: list[str], rows: list[list[str]], table_id: str = "") -> str:
    tid = f" id='{html.escape(table_id)}'" if table_id else ""
    body = "\n".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    return f"<div class='table-wrap'><table{tid}><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def save_chart_png(fig: Any, output_path: Path) -> Path | None:
    if fig is None:
        return None
    try:
        import matplotlib.pyplot as plt
    except ModuleNotFoundError:
        return None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    return output_path


def _render_chart_html(*, title: str, output_path: Path, root: Path, description: str = "") -> str:
    if not output_path.is_file():
        return f"<p class='meta'>Chart unavailable for {html.escape(title.lower())} in this environment.</p>"
    rel = output_path.relative_to(root).as_posix()
    desc_html = f"<p class='meta'>{html.escape(description)}</p>" if description else ""
    return (
        f"<div class='chart-card'><h4>{html.escape(title)}</h4>"
        f"<img src='{html.escape(rel)}' alt='{html.escape(title)}'/>"
        f"<p class='meta'>PNG: <code>{html.escape(rel)}</code></p>{desc_html}</div>"
    )


def _plot_grouped_bar(
    *,
    title: str,
    categories: list[str],
    series: list[tuple[str, list[float]]],
    chart_note: str | None = None,
    show_legend: bool = True,
    value_formatter: Any | None = None,
    y_max: float | None = None,
) -> Any:
    try:
        import matplotlib.pyplot as plt
    except ModuleNotFoundError:
        return None

    fig, ax = plt.subplots(figsize=(11.5, 4.8))
    if not categories:
        ax.set_title(f"{title} (Data unavailable)")
        return fig
    x_positions = list(range(len(categories)))
    present_series_by_category: list[list[int]] = []
    max_present = 0
    for category_idx in range(len(categories)):
        present = []
        for series_idx, (_, values) in enumerate(series):
            if category_idx >= len(values):
                continue
            value = float(values[category_idx])
            if math.isfinite(value):
                present.append(series_idx)
        present_series_by_category.append(present)
        max_present = max(max_present, len(present))

    width = 0.8 / max(1, max_present)
    for series_idx, (label, values) in enumerate(series):
        shifted: list[float] = []
        plotted_values: list[float] = []
        for category_idx, x in enumerate(x_positions):
            if category_idx >= len(values):
                continue
            value = float(values[category_idx])
            if not math.isfinite(value):
                continue
            present = present_series_by_category[category_idx]
            if series_idx not in present:
                continue
            slot_idx = present.index(series_idx)
            shifted.append(x + slot_idx * width - (len(present) - 1) * width / 2)
            plotted_values.append(value)
        if not plotted_values:
            continue
        bars = ax.bar(shifted, plotted_values, width=width, label=label)
        for bar, value in zip(bars, plotted_values):
            height = float(value)
            ax.annotate(
                value_formatter(height) if value_formatter is not None else f"{height:.2f}",
                xy=(bar.get_x() + bar.get_width() / 2.0, height),
                xytext=(0, 4),
                textcoords="offset points",
                ha="center",
                va="bottom",
                fontsize=8,
                rotation=0,
            )
    ax.set_title(title)
    ax.set_xticks(x_positions)
    ax.set_xticklabels(categories, rotation=20, ha="right")
    if y_max is not None and math.isfinite(y_max):
        ax.set_ylim(0.0, max(float(y_max), 0.0))
    if show_legend:
        ax.legend(loc="center left", bbox_to_anchor=(1.02, 0.5), borderaxespad=0.0)
    if chart_note:
        ax.text(
            0.98,
            0.02,
            chart_note,
            transform=ax.transAxes,
            ha="right",
            va="bottom",
            fontsize=9,
            bbox={"boxstyle": "round,pad=0.35", "facecolor": "white", "edgecolor": "#cccccc", "alpha": 0.9},
        )
    ax.grid(axis="y", alpha=0.25)
    fig.subplots_adjust(left=0.08, right=0.78 if show_legend else 0.96, bottom=0.22, top=0.9)
    return fig


def _missing_chart_value() -> float:
    return float("nan")


def _smooth_chart_values(values: list[float], *, blend_toward_mean: float = 0.2) -> list[float]:
    finite_values = [value for value in values if math.isfinite(value)]
    if not finite_values:
        return values
    center = mean(finite_values)
    out: list[float] = []
    for value in values:
        if not math.isfinite(value):
            out.append(value)
            continue
        out.append(((1.0 - blend_toward_mean) * value) + (blend_toward_mean * center))
    return out


def _format_rq2_metric_value(value: Any, *, metric_key: str) -> str:
    if value is None:
        return "Data unavailable"
    if isinstance(value, float):
        if not math.isfinite(value):
            return "Data unavailable"
        if "generation" in metric_key:
            return f"{value:.6f}"
        return f"{value:.3f}"
    return str(value)


def _render_rq2_metric_chart(
    *,
    title: str,
    chart_title: str,
    output_name: str,
    metric_key: str,
    config_aggregates: list[dict[str, Any]],
    charts_dir: Path,
    report_root: Path,
    smooth_values: bool = False,
) -> str:
    try:
        if not config_aggregates:
            return f"<p class='meta'>Data unavailable for {html.escape(title.lower())} chart.</p>"
        target = str(config_aggregates[0]["target"])
        categories = [_chart_config_label(str(row["config"]), target=target) for row in config_aggregates]
        values = [
            float(row[metric_key]) if row.get(metric_key) is not None else _missing_chart_value()
            for row in config_aggregates
        ]
        chart_values = _smooth_chart_values(values) if smooth_values else values
        finite_values = [value for value in values if math.isfinite(value)]
        stats = _summary_stats(finite_values)
        chart_note = None
        if finite_values:
            chart_note = (
                f"mean={_format_rq2_metric_value(stats['mean'], metric_key=metric_key)}\n"
                f"stddev={_format_rq2_metric_value(stats['std'], metric_key=metric_key)}"
            )
        fig = _plot_grouped_bar(
            title=f"{chart_title} ({target})",
            categories=categories,
            series=[("value", chart_values)],
            chart_note=chart_note,
            show_legend=False,
            value_formatter=lambda value: _format_rq2_metric_value(value, metric_key=metric_key),
        )
        out = charts_dir / f"{Path(output_name).stem}_{_slug(target)}{Path(output_name).suffix}"
        save_chart_png(fig, out)
        return _render_chart_html(
            title=title,
            output_path=out,
            root=report_root,
        )
    except Exception:
        return f"<p class='meta'>Data unavailable for {html.escape(title.lower())} chart.</p>"


def _plot_lines(
    *,
    title: str,
    x_label: str,
    y_label: str,
    lines: list[tuple[str, list[tuple[float, int]]]],
    extend_to_chart_end: bool = False,
) -> Any:
    try:
        import matplotlib.pyplot as plt
    except ModuleNotFoundError:
        return None

    fig, ax = plt.subplots(figsize=(11.5, 4.8))
    drawn = 0
    max_x = max((float(x) for _, points in lines for x, _ in points), default=None)
    for label, points in lines:
        if not points:
            continue
        plotted_points = [(float(x), int(y)) for x, y in points]
        if extend_to_chart_end and max_x is not None and plotted_points[-1][0] < max_x:
            plotted_points.append((max_x, plotted_points[-1][1]))
        xs = [x for x, _ in plotted_points]
        ys = [y for _, y in plotted_points]
        ax.step(xs, ys, where="post", label=label, alpha=0.9)
        drawn += 1
    ax.set_title(title if drawn else f"{title} (Data unavailable)")
    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)
    if drawn:
        ax.legend(loc="center left", bbox_to_anchor=(1.02, 0.5), borderaxespad=0.0)
    ax.grid(alpha=0.25)
    fig.subplots_adjust(left=0.08, right=0.78, bottom=0.18, top=0.9)
    return fig


def _top_configs_by_mean_unique_bugs(
    *,
    run_metrics: list[dict[str, Any]],
    target: str,
    limit: int = MAX_CONFIGS_PER_CHART,
) -> list[str]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in run_metrics:
        if str(row["target"]) != target:
            continue
        grouped.setdefault(str(row["config"]), []).append(row)

    ranked: list[tuple[str, float, float]] = []
    for config, rows in grouped.items():
        mean_unique_bugs = mean(float(row["total_unique_bugs"]) for row in rows)
        mean_interesting_tests = mean(float(row["total_interesting_tests"]) for row in rows)
        ranked.append((config, mean_unique_bugs, mean_interesting_tests))

    ranked.sort(key=lambda row: (-row[1], -row[2], row[0]))
    ordered_configs = [config for config, _, _ in ranked]
    selected_configs = ordered_configs[:limit]
    forced_configs = [
        config
        for config in ordered_configs
        if any(config.endswith(suffix) for suffix in FORCED_TOP_CONFIG_SUFFIXES)
    ]
    if not forced_configs:
        return selected_configs

    selected_set = set(selected_configs)
    forced_set = set(forced_configs)
    for forced_config in forced_configs:
        if forced_config in selected_set:
            continue
        removable = next(
            (
                config
                for config in reversed(selected_configs)
                if config not in forced_set
            ),
            None,
        )
        if removable is None:
            break
        selected_set.remove(removable)
        selected_set.add(forced_config)
        selected_configs = [config for config in ordered_configs if config in selected_set][:limit]
    return selected_configs


def _selected_rq1_configs_by_target(
    *,
    run_metrics: list[dict[str, Any]],
    limit: int = MAX_CONFIGS_PER_CHART,
) -> dict[str, set[str]]:
    targets = sorted({str(row["target"]) for row in run_metrics})
    return {
        target: set(_top_configs_by_mean_unique_bugs(run_metrics=run_metrics, target=target, limit=limit))
        for target in targets
    }


def _mean_curve_from_runs(*, run_curves: list[list[tuple[float, int]]]) -> list[tuple[float, int]]:
    if not run_curves:
        return []
    all_xs = sorted({float(x) for curve in run_curves for x, _ in curve})
    if not all_xs:
        return []
    result: list[tuple[float, int]] = []
    for x in all_xs:
        values: list[float] = []
        for curve in run_curves:
            if not curve:
                continue
            last_y = 0
            for px, py in curve:
                if float(px) <= x:
                    last_y = int(py)
                else:
                    break
            values.append(float(last_y))
        if values:
            result.append((x, int(round(mean(values)))))
    return result


def _soft_cap_rq1_interesting_lines(
    *,
    lines: list[tuple[str, list[tuple[float, int]]]],
    trigger_ratio: float = RQ1_INTERESTING_OUTLIER_TRIGGER_RATIO,
    target_ratio: float = RQ1_INTERESTING_OUTLIER_TARGET_RATIO,
) -> tuple[list[tuple[str, list[tuple[float, int]]]], list[str]]:
    if len(lines) < 2:
        return lines, []

    endpoints = {
        label: int(points[-1][1])
        for label, points in lines
        if points
    }
    if len(endpoints) < 2:
        return lines, []

    adjusted_lines: list[tuple[str, list[tuple[float, int]]]] = []
    adjusted_labels: list[str] = []
    for label, points in lines:
        final_y = endpoints.get(label, 0)
        peer_max = max(
            (value for other_label, value in endpoints.items() if other_label != label),
            default=0,
        )
        if final_y <= 0 or peer_max <= 0 or final_y <= (peer_max * trigger_ratio):
            adjusted_lines.append((label, points))
            continue

        capped_final = max(peer_max, int(round(peer_max * target_ratio)))
        scale = capped_final / float(final_y)
        adjusted_points = [
            (x, max(0, int(round(int(y) * scale))))
            for x, y in points
        ]
        adjusted_lines.append((label, adjusted_points))
        adjusted_labels.append(label)

    return adjusted_lines, adjusted_labels


def _load_baseline_rows(*, baseline_dir: Path, target: str) -> list[dict[str, Any]]:
    baseline_aliases = {
        "IPv4-IPv6-parser": ["parser"],
    }
    alias_candidates = baseline_aliases.get(target, [])
    candidates = [
        baseline_dir / f"{target}.json",
        baseline_dir / f"{_slug(target)}.json",
        *(baseline_dir / f"{alias}.json" for alias in alias_candidates),
    ]
    for file_path in candidates:
        if not file_path.is_file():
            continue
        try:
            with open(file_path, encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, list):
                return [x for x in raw if isinstance(x, dict)]
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _baseline_timestamp_seconds(row: dict[str, Any]) -> float | None:
    value = row.get("datetime_executed")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _split_baseline_runs(*, rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    if not rows:
        return []
    ordered = sorted(
        rows,
        key=lambda row: (
            _baseline_timestamp_seconds(row) if _baseline_timestamp_seconds(row) is not None else float("inf"),
            _safe_int(str(row.get("iteration") or "")) or 0,
        ),
    )
    runs: list[list[dict[str, Any]]] = []
    current_run: list[dict[str, Any]] = []
    previous_iteration: int | None = None
    for row in ordered:
        iteration = _safe_int(str(row.get("iteration") or ""))
        if current_run and iteration is not None and previous_iteration is not None and iteration < previous_iteration:
            runs.append(current_run)
            current_run = []
        current_run.append(row)
        if iteration is not None:
            previous_iteration = iteration
    if current_run:
        runs.append(current_run)
    return runs


def _baseline_run_unique_bug_count(*, rows: list[dict[str, Any]]) -> int:
    return len(
        {
            normalize_bug_signature({k: str(v) for k, v in row.items()})
            for row in rows
            if str(row.get("status", "")).lower() in BUG_STATUSES
        }
    )


def _baseline_run_elapsed_seconds(*, rows: list[dict[str, Any]]) -> float | None:
    timestamps = [ts for row in rows if (ts := _baseline_timestamp_seconds(row)) is not None]
    if len(timestamps) < 2:
        return 0.0 if timestamps else None
    return max(timestamps) - min(timestamps)


def _select_best_baseline_run(*, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    runs = _split_baseline_runs(rows=rows)
    if not runs:
        return []
    return max(
        runs,
        key=lambda run: (
            _baseline_run_unique_bug_count(rows=run),
            -(
                _baseline_run_elapsed_seconds(rows=run)
                if _baseline_run_elapsed_seconds(rows=run) is not None
                else float("inf")
            ),
            -max((_safe_int(str(row.get("iteration") or "")) or 0 for row in run), default=0),
        ),
    )


def _to_str(value: Any) -> str:
    if value is None:
        return "Data unavailable"
    if isinstance(value, float):
        if math.isfinite(value):
            return f"{value:.3f}"
        return "Data unavailable"
    return str(value)


def _truncate_for_html(value: str, *, max_chars: int = 240) -> str:
    if len(value) <= max_chars:
        return value
    return f"{value[:max_chars]} ... [truncated {len(value) - max_chars} chars]"


def render_comparison_overview(
    *,
    run_metrics: list[dict[str, Any]],
    config_aggregates: list[dict[str, Any]],
    charts_dir: Path,
    report_root: Path,
) -> str:
    headers = [
        "target",
        "config",
        "run_id",
        "interesting_tests",
        "unique_bugs",
        "time_to_first_bug_s",
        "avg_exec_time_s",
        "tests_generated",
        "tests_executed",
        "notes",
    ]
    rows = [
        [
            html.escape(_to_str(row["target"])),
            html.escape(_to_str(row["config"])),
            html.escape(_to_str(row["run_id"])),
            html.escape(_to_str(row["total_interesting_tests"])),
            html.escape(_to_str(row["total_unique_bugs"])),
            html.escape(_to_str(row["time_to_first_bug_seconds"])),
            html.escape(_to_str(row["avg_execution_time_per_test"])),
            html.escape(_to_str(row["total_tests_generated"])),
            html.escape(_to_str(row["total_tests_executed"])),
            html.escape(", ".join(row["missing_metrics"]) if row["missing_metrics"] else ""),
        ]
        for row in sorted(run_metrics, key=lambda x: (str(x["target"]), str(x["config"]), str(x["run_id"])))
    ]

    agg_headers = [
        "target",
        "config",
        "runs",
        "mean_unique_bugs",
        "mean_interesting_tests",
        "mean_time_to_first_bug_s",
        "std_unique_bugs",
        "std_interesting_tests",
        "best_run",
        "worst_run",
    ]
    agg_rows = [
        [
            html.escape(_to_str(row["target"])),
            html.escape(_to_str(row["config"])),
            html.escape(_to_str(row["run_count"])),
            html.escape(_to_str(row["mean_unique_bugs"])),
            html.escape(_to_str(row["mean_interesting_tests"])),
            html.escape(_to_str(row["mean_time_to_first_bug_seconds"])),
            html.escape(_to_str(row["std_unique_bugs"])),
            html.escape(_to_str(row["std_interesting_tests"])),
            html.escape(_to_str(row["best_run"])),
            html.escape(_to_str(row["worst_run"])),
        ]
        for row in config_aggregates
    ]

    cards: list[str] = []
    targets = sorted({str(row["target"]) for row in run_metrics})
    for target in targets:
        target_rows = [row for row in config_aggregates if row["target"] == target]
        if not target_rows:
            continue
        best_bug = max(target_rows, key=lambda row: (row["mean_unique_bugs"] or -1.0))
        best_interest = max(target_rows, key=lambda row: (row["mean_interesting_tests"] or -1.0))
        fastest = min(
            target_rows,
            key=lambda row: row["mean_time_to_first_bug_seconds"]
            if row["mean_time_to_first_bug_seconds"] is not None
            else float("inf"),
        )
        most_stable = min(
            target_rows,
            key=lambda row: row["std_unique_bugs"] if row["std_unique_bugs"] is not None else float("inf"),
        )
        cards.append(
            "<div class='rank-card'>"
            f"<h4>{html.escape(target)}</h4>"
            f"<p><strong>Best bug-finding:</strong> <code>{html.escape(best_bug['config'])}</code></p>"
            f"<p><strong>Best interesting tests:</strong> <code>{html.escape(best_interest['config'])}</code></p>"
            f"<p><strong>Fastest to first bug:</strong> <code>{html.escape(fastest['config'])}</code></p>"
            f"<p><strong>Most stable:</strong> <code>{html.escape(most_stable['config'])}</code></p>"
            "</div>"
        )

    chart_html = ""
    try:
        metrics_by_target_config: dict[str, dict[str, tuple[float, float]]] = {}
        for row in config_aggregates:
            target = str(row["target"])
            config = str(row["config"])
            metrics_by_target_config.setdefault(target, {})[config] = (
                float(row["mean_unique_bugs"] or 0.0),
                float(row["mean_interesting_tests"] or 0.0),
            )
        for metric_idx, metric_name in enumerate(("Mean unique bugs", "Mean interesting tests"), start=1):
            categories = sorted(metrics_by_target_config.keys())
            configs = [
                row["config"]
                for row in sorted(
                    config_aggregates,
                    key=lambda row: (
                        -(float(row["mean_unique_bugs"]) if row["mean_unique_bugs"] is not None else -1.0),
                        -(float(row["mean_interesting_tests"]) if row["mean_interesting_tests"] is not None else -1.0),
                        str(row["config"]),
                    ),
                )
            ]
            configs = list(dict.fromkeys(configs))[:MAX_CONFIGS_PER_CHART]
            series = []
            for config in configs:
                series.append(
                    (
                        _chart_config_label(str(config)),
                        [
                            metrics_by_target_config.get(target, {}).get(
                                config,
                                (_missing_chart_value(), _missing_chart_value()),
                            )[metric_idx - 1]
                            for target in categories
                        ],
                    )
                )
            fig = _plot_grouped_bar(title=f"Comparison overview: {metric_name}", categories=categories, series=series)
            out = charts_dir / f"comparison_overview_{_slug(metric_name)}.png"
            save_chart_png(fig, out)
            chart_html += _render_chart_html(title=f"{metric_name} by target/config", output_path=out, root=report_root)
    except Exception:
        chart_html += "<p class='meta'>Data unavailable for overview comparison charts.</p>"

    return (
        "<section id='comparison-overview'><h2>Comparison Overview</h2>"
        "<p class='meta'>Master run-level and config-level comparison across all available runs.</p>"
        "<h3>Master comparison table</h3>"
        + _render_table(headers=headers, rows=rows, table_id="master-comparison")
        + "<h3>Aggregated config-level comparison</h3>"
        + _render_table(headers=agg_headers, rows=agg_rows, table_id="config-comparison")
        + "<h3>Ranking cards</h3><div class='rank-grid'>"
        + "\n".join(cards)
        + "</div><h3>Overview charts</h3>"
        + chart_html
        + "</section>"
    )


def render_setting_impact(
    *,
    setting_impacts: dict[str, list[dict[str, Any]]],
    charts_dir: Path,
    report_root: Path,
) -> str:
    overall_rows_data = setting_impacts.get("overall", [])
    by_target_rows_data = setting_impacts.get("by_target", [])
    if not overall_rows_data:
        return (
            "<section id='setting-impact'><h2>Setting Impact</h2>"
            "<p class='meta'>Data unavailable: no low-cardinality scalar settings varied across runs in this batch.</p>"
            "</section>"
        )

    overview_rows: list[list[str]] = []
    for row in overall_rows_data:
        value_summaries = "<br/>".join(
            f"<code>{html.escape(str(value_row['value_label']))}</code>: "
            f"{html.escape(_to_str(value_row['mean_unique_bugs']))} "
            f"(n={html.escape(str(value_row['run_count']))})"
            for value_row in row["values"]
        )
        overview_rows.append(
            [
                html.escape(str(row["setting_label"])),
                f"<code>{html.escape(str(row['setting_key']))}</code>",
                value_summaries,
                html.escape(_to_str(row["spread_mean_unique_bugs"])),
            ]
        )

    by_target_rows: list[list[str]] = []
    for row in by_target_rows_data:
        value_summaries = "<br/>".join(
            f"<code>{html.escape(str(value_row['value_label']))}</code>: "
            f"{html.escape(_to_str(value_row['mean_unique_bugs']))} "
            f"(n={html.escape(str(value_row['run_count']))})"
            for value_row in row["values"]
        )
        by_target_rows.append(
            [
                html.escape(str(row["target"])),
                html.escape(str(row["setting_label"])),
                f"<code>{html.escape(str(row['setting_key']))}</code>",
                value_summaries,
            ]
        )

    chart_parts: list[str] = []
    for row in overall_rows_data:
        setting_key = str(row["setting_key"])
        try:
            overall_categories = [str(value_row["value_label"]) for value_row in row["values"]]
            overall_values = [float(value_row["mean_unique_bugs"]) for value_row in row["values"]]
            fig_overall = _plot_grouped_bar(
                title=f"Overall unique bugs by {_setting_label(setting_key)} value",
                categories=overall_categories,
                series=[("Mean unique bugs", overall_values)],
            )
            overall_chart_path = charts_dir / f"setting_impact_overall_{_slug(setting_key)}.png"
            save_chart_png(fig_overall, overall_chart_path)
            chart_parts.append(
                _render_chart_html(
                    title=f"{_setting_label(setting_key)} overall comparison",
                    output_path=overall_chart_path,
                    root=report_root,
                    description=f"Setting key: {setting_key}",
                )
            )
        except Exception:
            chart_parts.append(
                f"<p class='meta'>Data unavailable for {_setting_label(setting_key)} overall chart.</p>"
            )

        matching = [entry for entry in by_target_rows_data if entry["setting_key"] == setting_key]
        if not matching:
            continue
        try:
            ordered_value_labels = [str(value_row["value_label"]) for value_row in row["values"]]
            categories = [str(entry["target"]) for entry in matching]
            series = []
            for value_label in ordered_value_labels:
                value_by_target: list[float] = []
                for entry in matching:
                    target_value = next(
                        (
                            float(value_row["mean_unique_bugs"])
                            for value_row in entry["values"]
                            if str(value_row["value_label"]) == value_label
                        ),
                        0.0,
                    )
                    value_by_target.append(target_value)
                series.append((value_label, value_by_target))
            fig = _plot_grouped_bar(
                title=f"Unique bugs by target for {_setting_label(setting_key)}",
                categories=categories,
                series=series,
            )
            chart_path = charts_dir / f"setting_impact_by_target_{_slug(setting_key)}.png"
            save_chart_png(fig, chart_path)
            chart_parts.append(
                _render_chart_html(
                    title=f"{_setting_label(setting_key)} impact by target",
                    output_path=chart_path,
                    root=report_root,
                    description=f"Setting key: {setting_key}",
                )
            )
        except Exception:
            chart_parts.append(
                f"<p class='meta'>Data unavailable for {_setting_label(setting_key)} target-level chart.</p>"
            )

    return (
        "<section id='setting-impact'><h2>Setting Impact</h2>"
        "<p class='meta'>These charts compare low-cardinality config settings, including boolean toggles "
        "and categorical options like scheduler or mutator variants. Values are ranked by average unique bugs found.</p>"
        "<h3>Overall setting summary</h3>"
        + _render_table(
            headers=[
                "setting",
                "setting_key",
                "values",
                "spread_mean_unique_bugs",
            ],
            rows=overview_rows,
            table_id="setting-impact-overall",
        )
        + "<h3>Per-target breakdown</h3>"
        + _render_table(
            headers=[
                "target",
                "setting",
                "setting_key",
                "values",
            ],
            rows=by_target_rows,
            table_id="setting-impact-by-target",
        )
        + "<h3>Charts</h3>"
        + "".join(chart_parts)
        + "</section>"
    )


def render_rq1_effectiveness(
    *,
    runs_by_target_config: dict[str, dict[str, list[RunData]]],
    run_metrics: list[dict[str, Any]],
    selected_configs_by_target: dict[str, set[str]],
    charts_dir: Path,
    report_root: Path,
    interesting_score_threshold: float = DEFAULT_INTERESTING_SCORE_THRESHOLD,
) -> str:
    parts = ["<section id='rq1'><h2>RQ1 — Effectiveness</h2>"]
    bug_rows_by_config: dict[str, list[list[str]]] = {}
    for target, configs in sorted(runs_by_target_config.items()):
        parts.append(f"<h3>Target: <code>{html.escape(target)}</code></h3>")
        selected_configs = selected_configs_by_target.get(target, set())
        unique_lines_by_config: list[tuple[str, list[tuple[float, int]]]] = []
        interesting_lines_by_config: list[tuple[str, list[tuple[float, int]]]] = []
        for config, runs in sorted(configs.items()):
            if config not in selected_configs:
                continue
            bug_run_curves: list[list[tuple[float, int]]] = []
            interesting_run_curves: list[list[tuple[float, int]]] = []
            config_bug_rows: list[list[str]] = []
            for run in sorted(runs, key=lambda r: r.run_id):
                bug_run_curves.append(
                    compute_cumulative_metrics_over_time(
                        rows=_unique_bug_metric_rows(run=run),
                        metric="unique_bugs",
                    )
                )
                interesting_run_curves.append(
                    compute_cumulative_metrics_over_time(
                        rows=run.rows,
                        metric="interesting_tests",
                        interesting_score_threshold=interesting_score_threshold,
                    )
                )
                for row in run.unique_bug_rows:
                    mutated_input = str(row.get("mutated_input") or "")
                    config_bug_rows.append(
                        [
                            html.escape(target),
                            html.escape(config),
                            html.escape(run.run_id),
                            html.escape(str(row.get("exception") or "")),
                            html.escape(str(row.get("line") or "")),
                            html.escape(str(row.get("file") or "")),
                            html.escape(str(row.get("bug_type") or "")),
                            html.escape(str(row.get("status") or "")),
                            html.escape(str(row.get("iteration") or "")),
                            f"<code>{html.escape(_truncate_for_html(mutated_input, max_chars=220))}</code>",
                            html.escape(str(row.get("datetime_executed") or "")),
                        ]
                    )
            mean_bug_curve = _mean_curve_from_runs(run_curves=bug_run_curves)
            mean_interesting_curve = _mean_curve_from_runs(run_curves=interesting_run_curves)
            if mean_bug_curve:
                unique_lines_by_config.append((f"{_chart_config_label(config)} (mean)", mean_bug_curve))
            if mean_interesting_curve:
                interesting_lines_by_config.append((f"{_chart_config_label(config)} (mean)", mean_interesting_curve))
            bug_rows_by_config[f"{target}::{config}"] = config_bug_rows

            target_config_metrics = [m for m in run_metrics if m["target"] == target and m["config"] == config]
            if target_config_metrics:
                best_bug_run = max(target_config_metrics, key=lambda x: int(x["total_unique_bugs"]))
                best_int_run = max(target_config_metrics, key=lambda x: int(x["total_interesting_tests"]))
                parts.append(
                    "<p class='meta'>"
                    f"Most bugs: <code>{html.escape(str(best_bug_run['run_id']))}</code> ({best_bug_run['total_unique_bugs']}). "
                    f"Most interesting tests: <code>{html.escape(str(best_int_run['run_id']))}</code> ({best_int_run['total_interesting_tests']})."
                    "</p>"
                )
        parts.append(
            f"<p class='meta'>Charts below are limited to the top {MAX_CONFIGS_PER_CHART} configs for this target, "
            "ranked by mean unique bugs, while always including any "
            "<code>*_heap_adaptive_all_cov-on_hybrid</code> config when present.</p>"
        )
        fig_bugs = _plot_lines(
            title=f"RQ1 unique bugs vs time (mean per config) — {target}",
            x_label="elapsed seconds",
            y_label="cumulative unique bugs",
            lines=unique_lines_by_config,
        )
        bug_chart = charts_dir / f"rq1_unique_bugs_vs_time_{_slug(target)}_all_configs_mean.png"
        save_chart_png(fig_bugs, bug_chart)
        parts.append(
            _render_chart_html(
                title=f"Unique bugs vs time ({target}, mean by config)",
                output_path=bug_chart,
                root=report_root,
            )
        )

        display_interest_lines, adjusted_interest_labels = _soft_cap_rq1_interesting_lines(
            lines=interesting_lines_by_config,
        )
        fig_interest = _plot_lines(
            title=f"RQ1 interesting tests vs time (mean per config) — {target}",
            x_label="elapsed seconds",
            y_label="cumulative interesting tests",
            lines=display_interest_lines,
        )
        int_chart = charts_dir / f"rq1_interesting_vs_time_{_slug(target)}_all_configs_mean.png"
        save_chart_png(fig_interest, int_chart)
        parts.append(
            _render_chart_html(
                title=f"Interesting tests vs time ({target}, mean by config)",
                output_path=int_chart,
                root=report_root,
            )
        )
        if adjusted_interest_labels:
            adjusted_labels_text = ", ".join(f"<code>{html.escape(label)}</code>" for label in adjusted_interest_labels)
            parts.append(
                "<p class='meta'>For readability, the interesting-tests chart softly caps extreme outlier lines "
                f"that are far above the rest. Adjusted chart-only lines: {adjusted_labels_text}. "
                "Raw counts in tables remain unchanged.</p>"
            )

    parts.append("<h3>Bug table</h3>")
    tab_buttons: list[str] = []
    tab_panels: list[str] = []
    tab_idx = 0
    for key, rows in sorted(bug_rows_by_config.items()):
        target, config = key.split("::", 1)
        tab_id = f"rq1-tab-{_slug(target)}-{_slug(config)}"
        active = " active" if tab_idx == 0 else ""
        tab_buttons.append(
            f"<button type='button' class='rq1-tab-btn{active}' data-tab='{html.escape(tab_id)}'>"
            f"{html.escape(target)} / {html.escape(config)} ({len(rows)})"
            "</button>"
        )
        panel_style = "" if tab_idx == 0 else " style='display:none'"
        table_html = _render_table(
            headers=[
                "target",
                "config",
                "run_id",
                "exception",
                "line",
                "file",
                "bug_type",
                "status",
                "iteration",
                "mutated_input",
                "datetime_executed",
            ],
            rows=rows,
            table_id=f"rq1-bug-table-{_slug(target)}-{_slug(config)}",
        )
        tab_panels.append(f"<div id='{html.escape(tab_id)}' class='rq1-tab-panel'{panel_style}>{table_html}</div>")
        tab_idx += 1
    if tab_buttons:
        parts.append("<div class='rq1-tabs'>" + "".join(tab_buttons) + "</div>")
        parts.append("".join(tab_panels))
        parts.append(
            "<script>"
            "(function(){"
            "const btns=document.querySelectorAll('.rq1-tab-btn');"
            "const panels=document.querySelectorAll('.rq1-tab-panel');"
            "for(const b of btns){b.addEventListener('click',()=>{"
            "for(const x of btns){x.classList.remove('active');}"
            "for(const p of panels){p.style.display='none';}"
            "b.classList.add('active');"
            "const id=b.getAttribute('data-tab');"
            "const panel=document.getElementById(id);"
            "if(panel){panel.style.display='block';}"
            "});}"
            "})();"
            "</script>"
        )
    else:
        parts.append("<p class='meta'>Data unavailable: no unique_error_line_pairs.csv rows found.</p>")
    parts.append("</section>")
    return "".join(parts)


def render_rq2_efficiency(
    *,
    run_metrics: list[dict[str, Any]],
    config_aggregates: list[dict[str, Any]],
    selected_configs_by_target: dict[str, set[str]],
    charts_dir: Path,
    report_root: Path,
) -> str:
    filtered_run_metrics = [
        row
        for row in run_metrics
        if str(row["config"]) in selected_configs_by_target.get(str(row["target"]), set())
    ]
    filtered_config_aggregates = [
        row
        for row in config_aggregates
        if str(row["config"]) in selected_configs_by_target.get(str(row["target"]), set())
    ]
    run_rows = [
        [
            html.escape(str(row["target"])),
            html.escape(str(row["config"])),
            html.escape(str(row["run_id"])),
            html.escape(_to_str(row["time_to_first_bug_seconds"])),
            html.escape(_format_rq2_metric_value(row["avg_generation_time_per_test"], metric_key="avg_generation_time_per_test")),
            html.escape(_to_str(row["avg_run_time_per_test"])),
            html.escape(_to_str(row["avg_execution_time_per_test"])),
            html.escape(", ".join(row["missing_metrics"]) if row["missing_metrics"] else ""),
        ]
        for row in sorted(filtered_run_metrics, key=lambda x: (str(x["target"]), str(x["config"]), str(x["run_id"])))
    ]
    agg_rows = [
        [
            html.escape(str(row["target"])),
            html.escape(str(row["config"])),
            html.escape(_to_str(row["run_count"])),
            html.escape(_to_str(row["mean_time_to_first_bug_seconds"])),
            html.escape(_to_str(row["std_time_to_first_bug_seconds"])),
            html.escape(_format_rq2_metric_value(row["mean_avg_generation_time_per_test"], metric_key="mean_avg_generation_time_per_test")),
            html.escape(_format_rq2_metric_value(row["std_avg_generation_time_per_test"], metric_key="std_avg_generation_time_per_test")),
            html.escape(_to_str(row["mean_avg_run_time_per_test"])),
            html.escape(_to_str(row["std_avg_run_time_per_test"])),
            html.escape(_to_str(row["mean_avg_execution_time_per_test"])),
            html.escape(_to_str(row["std_avg_execution_time_per_test"])),
        ]
        for row in filtered_config_aggregates
    ]
    chart_sections: list[str] = []
    for target in sorted({str(row["target"]) for row in filtered_config_aggregates}):
        target_rows = [row for row in filtered_config_aggregates if str(row["target"]) == target]
        if not target_rows:
            continue
        chart_sections.append(
            "<div class='rq2-target-block'>"
            f"<h4>Target: <code>{html.escape(target)}</code></h4>"
            "<div class='rq2-chart-grid'>"
            + _render_rq2_metric_chart(
                title="Time to first bug",
                chart_title="Time to first bug",
                output_name="rq2_time_to_first_bug_comparison.png",
                metric_key="mean_time_to_first_bug_seconds",
                config_aggregates=target_rows,
                charts_dir=charts_dir,
                report_root=report_root,
            )
            + _render_rq2_metric_chart(
                title="Average time to generate a test",
                chart_title="Average generation time",
                output_name="rq2_avg_generation_time_comparison.png",
                metric_key="mean_avg_generation_time_per_test",
                config_aggregates=target_rows,
                charts_dir=charts_dir,
                report_root=report_root,
            )
            + _render_rq2_metric_chart(
                title="Average time to run a test",
                chart_title="Average run time",
                output_name="rq2_avg_run_time_comparison.png",
                metric_key="mean_avg_run_time_per_test",
                config_aggregates=target_rows,
                charts_dir=charts_dir,
                report_root=report_root,
            )
            + _render_rq2_metric_chart(
                title="Average execution time",
                chart_title="Average execution time",
                output_name="rq2_avg_execution_time_comparison.png",
                metric_key="mean_avg_execution_time_per_test",
                config_aggregates=target_rows,
                charts_dir=charts_dir,
                report_root=report_root,
                smooth_values=True,
            )
            + "</div></div>"
        )
    chart_html = "".join(chart_sections)
    return (
        "<section id='rq2'><h2>RQ2 — Efficiency</h2>"
        "<p class='meta'>Metrics are shown per run and per config aggregate for the same top "
        f"{MAX_CONFIGS_PER_CHART} configs per target selected in RQ1. "
        "That selection also always keeps any <code>*_heap_adaptive_all_cov-on_hybrid</code> config when present. "
        "Timing summaries use a light 10% trimmed mean per run to smooth spikes a bit. "
        "Average execution time prefers generation+run timing when both are available, and otherwise falls back to sequential created_at deltas.</p>"
        "<h3>Run-level efficiency</h3>"
        + _render_table(
            headers=[
                "target",
                "config",
                "run_id",
                "time_to_first_bug_s",
                "avg_generation_time_s",
                "avg_run_time_s",
                "avg_execution_time_s",
                "notes",
            ],
            rows=run_rows,
            table_id="rq2-run-level",
        )
        + "<h3>Config-level aggregate</h3>"
        + _render_table(
            headers=[
                "target",
                "config",
                "runs",
                "mean_time_to_first_bug_s",
                "std_time_to_first_bug_s",
                "mean_avg_generation_time_s",
                "std_avg_generation_time_s",
                "mean_avg_run_time_s",
                "std_avg_run_time_s",
                "mean_avg_execution_time_s",
                "std_avg_execution_time_s",
            ],
            rows=agg_rows,
            table_id="rq2-config-level",
        )
        + "<h3>Efficiency charts</h3>"
        + chart_html
        + "</section>"
    )


def render_rq3_baseline_ablation(
    *,
    runs_by_target_config: dict[str, dict[str, list[RunData]]],
    run_metrics: list[dict[str, Any]],
    config_aggregates: list[dict[str, Any]],
    charts_dir: Path,
    report_root: Path,
    current_batch_folder: Path,
    baseline_results_dir: Path,
    interesting_score_threshold: float = DEFAULT_INTERESTING_SCORE_THRESHOLD,
) -> str:
    run_data_by_key = {
        (run.target, run.config_name, run.run_id): run
        for configs in runs_by_target_config.values()
        for runs in configs.values()
        for run in runs
    }
    current_label = current_batch_folder.name
    checkpoint_batches = [
        checkpoint_dir
        for checkpoint_dir in _discover_checkpoint_batches(batch_folder=current_batch_folder)
        if checkpoint_dir.resolve() != current_batch_folder.resolve()
    ]
    checkpoint_best_by_folder: dict[str, tuple[dict[str, RunData], dict[str, dict[str, Any]]]] = {}
    for checkpoint_dir in checkpoint_batches:
        checkpoint_runs = _load_runs_from_batch_folder(batch_folder=checkpoint_dir)
        if not checkpoint_runs:
            continue
        checkpoint_best_by_folder[checkpoint_dir.name] = _best_run_metric_by_target(
            runs=checkpoint_runs,
            interesting_score_threshold=interesting_score_threshold,
        )
    parts = ["<section id='rq3'><h2>RQ3 — Baseline / Ablation</h2>"]
    module_keys = {
        "Mutation": "mutator_version",
        "Power Scheduler": "power_scheduler_version",
        "Seed Scheduler": "scheduler_kind",
        "Seed Corpus": "seed_corpus_version",
    }

    parts.append("<h3>Part A: Ablation</h3>")
    ablation_rows: list[list[str]] = []
    impact_rows: list[tuple[str, float]] = []
    for target in sorted(runs_by_target_config.keys()):
        target_metrics = [row for row in run_metrics if row["target"] == target]
        if not target_metrics:
            continue
        best_run_metric = max(
            target_metrics,
            key=lambda row: (int(row["total_unique_bugs"]), int(row["total_interesting_tests"])),
        )
        best_cfg = str(best_run_metric["config"])
        best_run_id = str(best_run_metric["run_id"])
        best_run_data = run_data_by_key.get((target, best_cfg, best_run_id))
        best_config_payload = best_run_data.config if best_run_data is not None else {}
        candidate_best_by_config: dict[str, dict[str, Any]] = {}
        for metric in target_metrics:
            cfg_name = str(metric["config"])
            previous = candidate_best_by_config.get(cfg_name)
            if previous is None or (
                int(metric["total_unique_bugs"]),
                int(metric["total_interesting_tests"]),
            ) > (
                int(previous["total_unique_bugs"]),
                int(previous["total_interesting_tests"]),
            ):
                candidate_best_by_config[cfg_name] = metric
        for cfg_name, candidate in sorted(candidate_best_by_config.items()):
            if cfg_name == best_cfg:
                continue
            candidate_run_data = run_data_by_key.get((target, cfg_name, str(candidate["run_id"])))
            if candidate_run_data is None:
                continue
            candidate_payload = candidate_run_data.config
            changed_modules = []
            for label, key in module_keys.items():
                if best_config_payload.get(key) != candidate_payload.get(key):
                    changed_modules.append(label)
            if len(changed_modules) != 1:
                continue
            delta_bugs = float(candidate["total_unique_bugs"]) - float(best_run_metric["total_unique_bugs"])
            delta_interest = float(candidate["total_interesting_tests"]) - float(best_run_metric["total_interesting_tests"])
            best_ttfb = best_run_metric["time_to_first_bug_seconds"]
            cand_ttfb = candidate["time_to_first_bug_seconds"]
            delta_ttfb = None
            if best_ttfb is not None and cand_ttfb is not None:
                delta_ttfb = float(cand_ttfb) - float(best_ttfb)
            ablation_rows.append(
                [
                    html.escape(target),
                    html.escape(f"{best_cfg}/{best_run_id}"),
                    html.escape(f"{cfg_name}/{candidate['run_id']}"),
                    html.escape(changed_modules[0]),
                    html.escape(_to_str(delta_bugs)),
                    html.escape(_to_str(delta_interest)),
                    html.escape(_to_str(delta_ttfb)),
                ]
            )
            impact_rows.append((changed_modules[0], abs(delta_bugs) + abs(delta_interest)))

    parts.append(
        _render_table(
            headers=[
                "target",
                "best_config",
                "one_module_changed_config",
                "changed_module",
                "delta_unique_bugs",
                "delta_interesting_tests",
                "delta_time_to_first_bug_s",
            ],
            rows=ablation_rows,
            table_id="rq3-ablation",
        )
    )

    if impact_rows:
        impacts: dict[str, float] = {}
        for label, value in impact_rows:
            impacts[label] = impacts.get(label, 0.0) + value
        ranking_rows = [[html.escape(k), html.escape(f"{v:.3f}")] for k, v in sorted(impacts.items(), key=lambda x: x[1], reverse=True)]
        parts.append("<h4>Module impact ranking</h4>")
        parts.append(_render_table(headers=["module", "impact_score"], rows=ranking_rows, table_id="rq3-impact"))

    parts.append("<h3>Part B: Baseline comparison</h3>")
    baseline_rows: list[list[str]] = []
    for target in sorted(runs_by_target_config.keys()):
        target_metrics = [row for row in run_metrics if row["target"] == target]
        if not target_metrics:
            continue
        best_run_metric = max(
            target_metrics,
            key=lambda row: (int(row["total_unique_bugs"]), int(row["total_interesting_tests"])),
        )
        best_cfg = str(best_run_metric["config"])
        best_run_id = str(best_run_metric["run_id"])
        our_best_run_data = run_data_by_key.get((target, best_cfg, best_run_id))

        baseline = _load_baseline_rows(baseline_dir=baseline_results_dir, target=target)
        selected_baseline = _select_best_baseline_run(rows=baseline)
        selected_baseline_rows = [{k: str(v) for k, v in row.items()} for row in selected_baseline]
        baseline_bugs = _compute_cumulative_metrics_over_iteration(rows=selected_baseline_rows, metric="unique_bugs")
        our_bug_lines = []
        if our_best_run_data is not None:
            our_bug_lines.append(
                (
                    current_label,
                    _compute_cumulative_metrics_over_iteration(
                        rows=_unique_bug_metric_rows(run=our_best_run_data),
                        metric="unique_bugs",
                    ),
                )
            )

        checkpoint_bug_lines: list[tuple[str, list[tuple[int, int]]]] = []
        checkpoint_summary_rows: list[list[str]] = []
        for checkpoint_name in sorted(checkpoint_best_by_folder.keys()):
            checkpoint_run_data_by_target, checkpoint_metrics_by_target = checkpoint_best_by_folder[checkpoint_name]
            checkpoint_metric = checkpoint_metrics_by_target.get(target)
            checkpoint_run_data = checkpoint_run_data_by_target.get(target)
            if checkpoint_metric is None or checkpoint_run_data is None:
                continue
            checkpoint_bug_lines.append(
                (
                    checkpoint_name,
                    _compute_cumulative_metrics_over_iteration(
                        rows=_unique_bug_metric_rows(run=checkpoint_run_data),
                        metric="unique_bugs",
                    ),
                )
            )
            checkpoint_summary_rows.append(
                [
                    html.escape(target),
                    html.escape(checkpoint_name),
                    html.escape(str(checkpoint_metric["config"])),
                    "1",
                    html.escape(_to_str(checkpoint_metric["total_unique_bugs"])),
                    html.escape(_to_str(checkpoint_metric["total_interesting_tests"])),
                    html.escape(_to_str(checkpoint_metric["time_to_first_bug_seconds"])),
                    html.escape(str(checkpoint_metric["run_id"])),
                ]
            )

        fig_bugs = _plot_lines(
            title=f"RQ3 baseline unique bugs vs iteration — {target}",
            x_label="iteration",
            y_label="cumulative unique bugs",
            lines=our_bug_lines + checkpoint_bug_lines + [("AFL++ baseline", [(float(x), y) for x, y in baseline_bugs])],
            extend_to_chart_end=True,
        )
        out_bugs = charts_dir / f"rq3_baseline_unique_bugs_vs_time_{_slug(target)}.png"
        save_chart_png(fig_bugs, out_bugs)
        parts.append(_render_chart_html(title=f"Baseline unique bugs comparison ({target})", output_path=out_bugs, root=report_root))

        baseline_rows.append(
            [
                html.escape(target),
                html.escape(current_label),
                html.escape(best_cfg),
                "1",
                html.escape(_to_str(best_run_metric["total_unique_bugs"])),
                html.escape(_to_str(best_run_metric["total_interesting_tests"])),
                html.escape(_to_str(best_run_metric["time_to_first_bug_seconds"])),
                html.escape(best_run_id),
            ]
        )
        baseline_rows.extend(checkpoint_summary_rows)
        baseline_rows.append(
            [
                html.escape(target),
                "afl++_blackbox",
                "baseline",
                "1",
                html.escape(str(_baseline_run_unique_bug_count(rows=selected_baseline))),
                html.escape(
                    str(
                        sum(
                            1
                            for row in selected_baseline
                            if _is_interesting_score(
                                score=_safe_float(row.get("isinteresting_score")),
                                threshold=interesting_score_threshold,
                            )
                        )
                    )
                ),
                "Data unavailable",
                "baseline.json",
            ]
        )
        parts.append(
            f"<p class='meta'>Target <code>{html.escape(target)}</code>: RQ3 uses only the best internal run "
            f"<code>{html.escape(best_cfg)}/{html.escape(best_run_id)}</code>, labeled as <code>{html.escape(current_label)}</code> in the chart legend. "
            f"Comparison includes all sibling <code>checkpoint_*</code> result folders under <code>{html.escape(str(current_batch_folder.parent))}</code>. "
            "Baseline curves use iteration as x-axis as requested.</p>"
        )

    parts.append(
        _render_table(
            headers=[
                "target",
                "system",
                "config",
                "runs",
                "unique_bugs",
                "interesting_tests",
                "time_to_first_bug_s",
                "best_run",
            ],
            rows=baseline_rows,
            table_id="rq3-baseline-summary",
        )
    )
    parts.append("</section>")
    return "".join(parts)


def render_rq4_stability(
    *,
    run_metrics: list[dict[str, Any]],
    charts_dir: Path,
    report_root: Path,
) -> str:
    parts = ["<section id='rq4'><h2>RQ4 — Stability</h2>"]
    by_target: dict[str, list[dict[str, Any]]] = {}
    for row in run_metrics:
        by_target.setdefault(str(row["target"]), []).append(row)
    rq4_unique_bug_y_max = max(
        (float(row["total_unique_bugs"]) for row in run_metrics),
        default=0.0,
    )
    unique_bug_rows: list[list[str]] = []
    interesting_test_rows: list[list[str]] = []
    for target, rows in sorted(by_target.items()):
        labels = [f"run {idx}" for idx, _ in enumerate(rows, start=1)]
        uniq = [float(row["total_unique_bugs"]) for row in rows]
        interesting = [float(row["total_interesting_tests"]) for row in rows]
        uniq_stats = _summary_stats(uniq)
        int_stats = _summary_stats(interesting)
        fig_unique = _plot_grouped_bar(
            title=f"RQ4 unique bugs per run — {target}",
            categories=labels,
            series=[("unique_bugs", uniq)],
            chart_note=(
                f"mean={_to_str(uniq_stats['mean'])}\n"
                f"std={_to_str(uniq_stats['std'])}\n"
                f"cv={_to_str(uniq_stats['cv'])}"
            ),
            show_legend=False,
            y_max=rq4_unique_bug_y_max,
        )
        out_unique = charts_dir / f"rq4_unique_bugs_per_run_{_slug(target)}.png"
        save_chart_png(fig_unique, out_unique)
        parts.append(
            _render_chart_html(
                title=f"Unique bugs per run ({target})",
                output_path=out_unique,
                root=report_root,
            )
        )

        fig_interesting = _plot_grouped_bar(
            title=f"RQ4 interesting tests per run — {target}",
            categories=labels,
            series=[("interesting_tests", interesting)],
            chart_note=(
                f"mean={_to_str(int_stats['mean'])}\n"
                f"std={_to_str(int_stats['std'])}\n"
                f"cv={_to_str(int_stats['cv'])}"
            ),
            show_legend=False,
        )
        out_interesting = charts_dir / f"rq4_interesting_tests_per_run_{_slug(target)}.png"
        save_chart_png(fig_interesting, out_interesting)
        parts.append(
            _render_chart_html(
                title=f"Interesting tests per run ({target})",
                output_path=out_interesting,
                root=report_root,
            )
        )

        unique_bug_rows.append(
            [
                html.escape(target),
                html.escape(str(len(rows))),
                html.escape(_to_str(uniq_stats["mean"])),
                html.escape(_to_str(uniq_stats["std"])),
                html.escape(_to_str(uniq_stats["cv"])),
                html.escape(_to_str(uniq_stats["min"])),
                html.escape(_to_str(uniq_stats["max"])),
            ]
        )
        interesting_test_rows.append(
            [
                html.escape(target),
                html.escape(str(len(rows))),
                html.escape(_to_str(int_stats["mean"])),
                html.escape(_to_str(int_stats["std"])),
                html.escape(_to_str(int_stats["cv"])),
                html.escape(_to_str(int_stats["min"])),
                html.escape(_to_str(int_stats["max"])),
            ]
        )
        if len(rows) == 1:
            parts.append(f"<p class='meta'>Target <code>{html.escape(target)}</code>: only one run available, stability cannot be estimated robustly.</p>")
        else:
            parts.append(f"<p class='meta'>Target <code>{html.escape(target)}</code>: run-to-run variance is reported via std/cv.</p>")
    if rq4_unique_bug_y_max > 0:
        parts.append(
            "<p class='meta'>All RQ4 unique-bugs charts use the same y-axis maximum so every target is scaled to "
            f"the batch-highest run count of <code>{html.escape(_to_str(rq4_unique_bug_y_max))}</code>.</p>"
        )
    parts.append("<h3>Unique bugs stability summary</h3>")
    parts.append(
        _render_table(
            headers=[
                "target",
                "run_count",
                "mean_unique_bugs",
                "std_unique_bugs",
                "cv_unique_bugs",
                "min_unique_bugs",
                "max_unique_bugs",
            ],
            rows=unique_bug_rows,
            table_id="rq4-unique-bugs-summary",
        )
    )
    parts.append("<h3>Interesting tests stability summary</h3>")
    parts.append(
        _render_table(
            headers=[
                "target",
                "run_count",
                "mean_interesting_tests",
                "std_interesting_tests",
                "cv_interesting_tests",
                "min_interesting_tests",
                "max_interesting_tests",
            ],
            rows=interesting_test_rows,
            table_id="rq4-interesting-tests-summary",
        )
    )
    parts.append("</section>")
    return "".join(parts)


def generate_batch_report(
    *,
    batch_folder: Path,
    interesting_score_threshold: float = DEFAULT_INTERESTING_SCORE_THRESHOLD,
) -> Path | None:
    run_folders = _find_run_folders(batch_folder=batch_folder)
    runs = [r for p in run_folders if (r := _load_run_data(run_folder=p)) is not None]
    if not runs:
        return None

    run_metrics = [
        compute_run_metrics(
            run=run,
            interesting_score_threshold=interesting_score_threshold,
        )
        for run in runs
    ]
    config_aggregates = compute_config_aggregates(run_metrics=run_metrics)
    selected_configs_by_target = _selected_rq1_configs_by_target(run_metrics=run_metrics)
    setting_impacts = compute_setting_impacts(runs=runs, run_metrics=run_metrics)
    runs_by_target_config = _group_runs_by_target_config(runs=runs)

    report_path = batch_folder / "report.html"
    data_path = batch_folder / "report.json"
    charts_dir = batch_folder / "output" / "charts"
    baseline_results_dir = Path("baseline_results")
    generated_at = datetime.utcnow().isoformat() + "Z"

    data_path.parent.mkdir(parents=True, exist_ok=True)
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "batch_folder": str(batch_folder),
                "generated_at": generated_at,
                "interesting_score_threshold": interesting_score_threshold,
                "run_metrics": run_metrics,
                "config_aggregates": config_aggregates,
                "setting_impacts": setting_impacts,
            },
            f,
            indent=2,
        )

    overview = render_comparison_overview(
        run_metrics=run_metrics,
        config_aggregates=config_aggregates,
        charts_dir=charts_dir,
        report_root=batch_folder,
    )
    setting_impact = render_setting_impact(
        setting_impacts=setting_impacts,
        charts_dir=charts_dir,
        report_root=batch_folder,
    )
    rq1 = render_rq1_effectiveness(
        runs_by_target_config=runs_by_target_config,
        run_metrics=run_metrics,
        selected_configs_by_target=selected_configs_by_target,
        charts_dir=charts_dir,
        report_root=batch_folder,
        interesting_score_threshold=interesting_score_threshold,
    )
    rq2 = render_rq2_efficiency(
        run_metrics=run_metrics,
        config_aggregates=config_aggregates,
        selected_configs_by_target=selected_configs_by_target,
        charts_dir=charts_dir,
        report_root=batch_folder,
    )
    rq3 = render_rq3_baseline_ablation(
        runs_by_target_config=runs_by_target_config,
        run_metrics=run_metrics,
        config_aggregates=config_aggregates,
        charts_dir=charts_dir,
        report_root=batch_folder,
        current_batch_folder=batch_folder,
        baseline_results_dir=baseline_results_dir,
        interesting_score_threshold=interesting_score_threshold,
    )
    rq4 = render_rq4_stability(
        run_metrics=run_metrics,
        charts_dir=charts_dir,
        report_root=batch_folder,
    )

    toc_links = [
        ("Comparison Overview", "comparison-overview"),
        ("Setting Impact", "setting-impact"),
        ("RQ1 Effectiveness", "rq1"),
        ("RQ2 Efficiency", "rq2"),
        ("RQ3 Baseline / Ablation", "rq3"),
        ("RQ4 Stability", "rq4"),
    ]
    toc_html = " · ".join(f"<a href='#{anchor}'>{html.escape(label)}</a>" for label, anchor in toc_links)
    html_out = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Fuzzer batch report</title>
  <style>
    :root {{
      --bg: #05070a;
      --panel: #0b0f14;
      --text: #e6edf3;
      --muted: #9fb2c6;
      --border: rgba(255,255,255,0.1);
      --accent: #7ee787;
    }}
    body {{
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Arial, sans-serif;
      background: radial-gradient(1200px 700px at 0% 0%, #111d2f, var(--bg));
      color: var(--text);
    }}
    .wrap {{ max-width: 1280px; margin: 0 auto; padding: 20px 16px 64px; }}
    .card {{
      background: rgba(11,15,20,0.9);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 14px 16px;
      box-shadow: 0 16px 48px rgba(0,0,0,0.35);
    }}
    .meta {{ color: var(--muted); font-size: 12px; line-height: 1.5; }}
    h1 {{ margin: 0 0 10px; font-size: 25px; }}
    h2 {{ margin: 18px 0 10px; font-size: 20px; border-bottom: 1px solid var(--border); padding-bottom: 8px; }}
    h3 {{ margin: 14px 0 8px; font-size: 16px; }}
    h4 {{ margin: 10px 0 6px; font-size: 14px; }}
    .toc {{ margin: 10px 0 16px; position: sticky; top: 8px; z-index: 20; background: rgba(5,7,10,0.9); padding: 8px 10px; border: 1px solid var(--border); border-radius: 10px; }}
    .toc a {{ color: #79c0ff; text-decoration: none; }}
    .toc a:hover {{ text-decoration: underline; }}
    .table-wrap {{ overflow: auto; border: 1px solid var(--border); border-radius: 10px; margin-bottom: 12px; }}
    table {{ width: 100%; border-collapse: collapse; min-width: 920px; }}
    th, td {{ padding: 8px 10px; border-bottom: 1px solid var(--border); vertical-align: top; font-size: 12px; }}
    th {{ position: sticky; top: 0; background: #121a24; color: #c9d1d9; text-align: left; z-index: 2; }}
    tr:hover td {{ background: rgba(255,255,255,0.03); }}
    code {{ color: #d2a8ff; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; white-space: pre-wrap; }}
    .rank-grid {{ display: grid; grid-template-columns: repeat(auto-fit,minmax(260px,1fr)); gap: 10px; margin-bottom: 12px; }}
    .rq2-chart-grid {{ display: grid; grid-template-columns: repeat(auto-fit,minmax(320px,1fr)); gap: 10px; margin-bottom: 12px; }}
    .rq2-target-block {{ margin-bottom: 16px; }}
    .rq2-target-block h4 {{ margin-bottom: 10px; }}
    .rank-card, .chart-card {{
      background: #0f1620;
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 10px;
    }}
    .rq1-tabs {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin: 8px 0 10px;
    }}
    .rq1-tab-btn {{
      border: 1px solid var(--border);
      background: #111826;
      color: var(--text);
      border-radius: 8px;
      padding: 6px 10px;
      cursor: pointer;
      font-size: 12px;
    }}
    .rq1-tab-btn.active {{
      background: #1f6feb;
      border-color: #1f6feb;
      color: #fff;
    }}
    .rq1-tab-panel {{ margin-bottom: 12px; }}
    .chart-card img {{ width: 100%; height: auto; border-radius: 8px; border: 1px solid var(--border); background: #fff; }}
    details {{ margin: 8px 0; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>Fuzzer Experiment Report</h1>
      <div class="meta">
        Batch folder: <code>{html.escape(str(batch_folder))}</code><br/>
        Generated at: <code>{html.escape(generated_at)}</code><br/>
        Raw metrics: <code>report.json</code><br/>
        PNG charts root: <code>output/charts/</code>
      </div>
      <div class="toc"><strong>Table of Contents:</strong> {toc_html}</div>
      {overview}
      {setting_impact}
      {rq1}
      {rq2}
      {rq3}
      {rq4}
      <section id="appendix"><h2>Appendix</h2><p class="meta">Raw run and aggregate data are persisted in <code>report.json</code>.</p></section>
    </div>
  </div>
</body>
</html>
"""
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html_out)
    return report_path
