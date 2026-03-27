from __future__ import annotations

import csv
import html
import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean, pstdev
from typing import Any


BUG_STATUSES = {"bug", "crash", "timeout"}
INTERESTING_SCORE_THRESHOLD = 0.5


@dataclass(frozen=True)
class RunData:
    run_folder: Path
    target: str
    config_name: str
    run_id: str
    rows: list[dict[str, str]]
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


def _slug(value: str) -> str:
    safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-")
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe or "na"


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
    with open(runs_csv, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _load_unique_error_line_pairs_rows(run_folder: Path) -> list[dict[str, str]]:
    path = run_folder / "unique_error_line_pairs.csv"
    if not path.is_file():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _load_run_data(*, run_folder: Path) -> RunData | None:
    rows = _load_runs_csv_rows(run_folder / "runs.csv")
    if not rows:
        return None
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
            (row.get("target") or "").strip()
            for row in rows
            if (row.get("target") or "").strip()
        }
    )
    target = target_values[0] if target_values else "unknown"
    return RunData(
        run_folder=run_folder,
        target=target,
        config_name=run_folder.parent.name,
        run_id=run_folder.name,
        rows=rows,
        config=config,
    )


def _group_runs_by_target_config(*, runs: list[RunData]) -> dict[str, dict[str, list[RunData]]]:
    out: dict[str, dict[str, list[RunData]]] = {}
    for run in runs:
        out.setdefault(run.target, {}).setdefault(run.config_name, []).append(run)
    return out


def _collect_timestamps(rows: list[dict[str, str]]) -> list[datetime]:
    values: list[datetime] = []
    for row in rows:
        dt = _parse_iso8601(row.get("created_at"))
        if dt is not None:
            values.append(dt)
    return sorted(values)


def compute_cumulative_metrics_over_time(
    *, rows: list[dict[str, str]], metric: str
) -> list[tuple[float, int]]:
    if not rows:
        return []
    ordered = sorted(
        rows,
        key=lambda row: (
            _parse_iso8601(row.get("created_at")) or datetime.max,
            _safe_int(row.get("iteration")) or 0,
        ),
    )
    first_dt = None
    for row in ordered:
        candidate = _parse_iso8601(row.get("created_at"))
        if candidate is not None:
            first_dt = candidate
            break
    if first_dt is None:
        return []

    points: list[tuple[float, int]] = [(0.0, 0)]
    seen_bugs: set[str] = set()
    count = 0
    for row in ordered:
        dt = _parse_iso8601(row.get("created_at"))
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
            if score is None or score <= INTERESTING_SCORE_THRESHOLD:
                continue
            count += 1
        else:
            continue
        points.append((max((dt - first_dt).total_seconds(), 0.0), count))
    return points


def _compute_cumulative_metrics_over_iteration(
    *, rows: list[dict[str, str]], metric: str
) -> list[tuple[int, int]]:
    if not rows:
        return []
    ordered = sorted(rows, key=lambda row: _safe_int(row.get("iteration")) or 0)
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
            if score is None or score <= INTERESTING_SCORE_THRESHOLD:
                continue
            count += 1
        else:
            continue
        points.append((iteration, count))
    return points


def compute_run_metrics(*, run: RunData) -> dict[str, Any]:
    rows = run.rows
    total_generated = len(rows)
    total_executed = len(rows)
    bug_rows = [row for row in rows if (row.get("status") or "").strip().lower() in BUG_STATUSES]
    unique_bugs = {normalize_bug_signature(row) for row in bug_rows}
    interesting_tests = sum(
        1
        for row in rows
        if (_safe_float(row.get("isinteresting_score")) or 0.0) > INTERESTING_SCORE_THRESHOLD
    )
    timestamps = _collect_timestamps(rows)
    first_bug_time: float | None = None
    if timestamps:
        start = timestamps[0]
        bug_times = [_parse_iso8601(row.get("created_at")) for row in bug_rows]
        bug_times = [t for t in bug_times if t is not None]
        if bug_times:
            first_bug_time = max((min(bug_times) - start).total_seconds(), 0.0)

    avg_execution = None
    explicit_execution_times = [
        float(value)
        for value in (row.get("run_time_seconds") for row in rows)
        if value not in (None, "")
    ]
    if explicit_execution_times:
        avg_execution = sum(explicit_execution_times) / len(explicit_execution_times)
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
            avg_execution = sum(execution_deltas) / len(execution_deltas)

    missing: list[str] = []
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
        avg_execution_values = [
            float(r["avg_execution_time_per_test"])
            for r in rows
            if r["avg_execution_time_per_test"] is not None
        ]
        uniq_stats = _summary_stats(unique_values)
        int_stats = _summary_stats(interesting_values)
        ttfb_stats = _summary_stats(ttfb_values)
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
                "mean_avg_execution_time_per_test": avg_execution_stats["mean"],
                "std_unique_bugs": uniq_stats["std"],
                "std_interesting_tests": int_stats["std"],
                "std_time_to_first_bug_seconds": ttfb_stats["std"],
                "std_avg_execution_time_per_test": avg_execution_stats["std"],
                "best_run": best_row["run_id"],
                "worst_run": worst_row["run_id"],
            }
        )
    return out


def _render_table(*, headers: list[str], rows: list[list[str]], table_id: str = "") -> str:
    tid = f" id='{html.escape(table_id)}'" if table_id else ""
    body = "\n".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    return f"<div class='table-wrap'><table{tid}><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def save_chart_png(fig: Any, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=200, bbox_inches="tight")
    return output_path


def _render_chart_html(*, title: str, output_path: Path, root: Path, description: str = "") -> str:
    rel = output_path.relative_to(root).as_posix()
    desc_html = f"<p class='meta'>{html.escape(description)}</p>" if description else ""
    return (
        f"<div class='chart-card'><h4>{html.escape(title)}</h4>"
        f"<img src='{html.escape(rel)}' alt='{html.escape(title)}'/>"
        f"<p class='meta'>PNG: <code>{html.escape(rel)}</code></p>{desc_html}</div>"
    )


def _plot_grouped_bar(*, title: str, categories: list[str], series: list[tuple[str, list[float]]]) -> Any:
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 4.8))
    if not categories:
        ax.set_title(f"{title} (Data unavailable)")
        return fig
    width = 0.8 / max(1, len(series))
    x_positions = list(range(len(categories)))
    for idx, (label, values) in enumerate(series):
        shifted = [x + idx * width - (len(series) - 1) * width / 2 for x in x_positions]
        bars = ax.bar(shifted, values, width=width, label=label)
        for bar, value in zip(bars, values):
            height = float(value)
            if not math.isfinite(height):
                continue
            y = height + (0.01 * max(1.0, abs(height)))
            ax.text(
                bar.get_x() + bar.get_width() / 2.0,
                y,
                f"{height:.2f}",
                ha="center",
                va="bottom",
                fontsize=8,
                rotation=0,
            )
    ax.set_title(title)
    ax.set_xticks(x_positions)
    ax.set_xticklabels(categories, rotation=20, ha="right")
    ax.legend(loc="best")
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    return fig


def _plot_lines(
    *,
    title: str,
    x_label: str,
    y_label: str,
    lines: list[tuple[str, list[tuple[float, int]]]],
    extend_to_chart_end: bool = False,
) -> Any:
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 4.8))
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
        ax.legend(loc="best")
    ax.grid(alpha=0.25)
    fig.tight_layout()
    return fig


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
            configs = sorted({cfg for values in metrics_by_target_config.values() for cfg in values.keys()})
            series = []
            for config in configs:
                series.append(
                    (
                        config,
                        [metrics_by_target_config.get(target, {}).get(config, (0.0, 0.0))[metric_idx - 1] for target in categories],
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


def render_rq1_effectiveness(
    *,
    runs_by_target_config: dict[str, dict[str, list[RunData]]],
    run_metrics: list[dict[str, Any]],
    charts_dir: Path,
    report_root: Path,
) -> str:
    parts = ["<section id='rq1'><h2>RQ1 — Effectiveness</h2>"]
    bug_rows_by_config: dict[str, list[list[str]]] = {}
    for target, configs in sorted(runs_by_target_config.items()):
        parts.append(f"<h3>Target: <code>{html.escape(target)}</code></h3>")
        unique_lines_by_config: list[tuple[str, list[tuple[float, int]]]] = []
        interesting_lines_by_config: list[tuple[str, list[tuple[float, int]]]] = []
        for config, runs in sorted(configs.items()):
            bug_run_curves: list[list[tuple[float, int]]] = []
            interesting_run_curves: list[list[tuple[float, int]]] = []
            config_bug_rows: list[list[str]] = []
            for run in sorted(runs, key=lambda r: r.run_id):
                bug_run_curves.append(compute_cumulative_metrics_over_time(rows=run.rows, metric="unique_bugs"))
                interesting_run_curves.append(compute_cumulative_metrics_over_time(rows=run.rows, metric="interesting_tests"))
                pair_rows = _load_unique_error_line_pairs_rows(run.run_folder)
                for row in pair_rows:
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
                unique_lines_by_config.append((f"{config} (mean)", mean_bug_curve))
            if mean_interesting_curve:
                interesting_lines_by_config.append((f"{config} (mean)", mean_interesting_curve))
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

        fig_interest = _plot_lines(
            title=f"RQ1 interesting tests vs time (mean per config) — {target}",
            x_label="elapsed seconds",
            y_label="cumulative interesting tests",
            lines=interesting_lines_by_config,
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
    charts_dir: Path,
    report_root: Path,
) -> str:
    run_rows = [
        [
            html.escape(str(row["target"])),
            html.escape(str(row["config"])),
            html.escape(str(row["run_id"])),
            html.escape(_to_str(row["avg_execution_time_per_test"])),
            html.escape(", ".join(row["missing_metrics"]) if row["missing_metrics"] else ""),
        ]
        for row in sorted(run_metrics, key=lambda x: (str(x["target"]), str(x["config"]), str(x["run_id"])))
    ]
    agg_rows = [
        [
            html.escape(str(row["target"])),
            html.escape(str(row["config"])),
            html.escape(_to_str(row["run_count"])),
            html.escape(_to_str(row["mean_avg_execution_time_per_test"])),
            html.escape(_to_str(row["std_avg_execution_time_per_test"])),
        ]
        for row in config_aggregates
    ]
    chart_html = ""
    try:
        targets = sorted({str(row["target"]) for row in config_aggregates})
        configs = sorted({str(row["config"]) for row in config_aggregates})
        values_by_target: dict[str, dict[str, float]] = {target: {} for target in targets}
        for row in config_aggregates:
            val = row["mean_avg_execution_time_per_test"]
            values_by_target[str(row["target"])][str(row["config"])] = float(val) if val is not None else 0.0
        series = [(config, [values_by_target[target].get(config, 0.0) for target in targets]) for config in configs]
        fig = _plot_grouped_bar(title="RQ2 average execution time comparison", categories=targets, series=series)
        out = charts_dir / "rq2_avg_execution_time_comparison.png"
        save_chart_png(fig, out)
        chart_html = _render_chart_html(title="Average execution time by target/config", output_path=out, root=report_root)
    except Exception:
        chart_html = "<p class='meta'>Data unavailable for efficiency chart.</p>"
    return (
        "<section id='rq2'><h2>RQ2 — Efficiency</h2>"
        "<p class='meta'>Metrics are shown per run and per config aggregate. "
        "Average execution time is derived from sequential created_at timestamp deltas in each run.</p>"
        "<h3>Run-level efficiency</h3>"
        + _render_table(
            headers=[
                "target",
                "config",
                "run_id",
                "avg_execution_time_s",
                "notes",
            ],
            rows=run_rows,
            table_id="rq2-run-level",
        )
        + "<h3>Config-level aggregate</h3>"
        + _render_table(
            headers=["target", "config", "runs", "mean_avg_execution_time_s", "std_avg_execution_time_s"],
            rows=agg_rows,
            table_id="rq2-config-level",
        )
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
    baseline_results_dir: Path,
) -> str:
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
        target_aggs = [row for row in config_aggregates if row["target"] == target]
        if not target_aggs:
            continue
        best = max(target_aggs, key=lambda row: (row["mean_unique_bugs"] or -1.0, row["mean_interesting_tests"] or -1.0))
        best_cfg = best["config"]
        best_runs = runs_by_target_config.get(target, {}).get(str(best_cfg), [])
        best_config_payload = best_runs[0].config if best_runs else {}
        for candidate in target_aggs:
            cfg_name = str(candidate["config"])
            if cfg_name == best_cfg:
                continue
            candidate_runs = runs_by_target_config.get(target, {}).get(cfg_name, [])
            if not candidate_runs:
                continue
            candidate_payload = candidate_runs[0].config
            changed_modules = []
            for label, key in module_keys.items():
                if best_config_payload.get(key) != candidate_payload.get(key):
                    changed_modules.append(label)
            if len(changed_modules) != 1:
                continue
            delta_bugs = float(candidate["mean_unique_bugs"] or 0.0) - float(best["mean_unique_bugs"] or 0.0)
            delta_interest = float(candidate["mean_interesting_tests"] or 0.0) - float(best["mean_interesting_tests"] or 0.0)
            best_ttfb = best["mean_time_to_first_bug_seconds"]
            cand_ttfb = candidate["mean_time_to_first_bug_seconds"]
            delta_ttfb = None
            if best_ttfb is not None and cand_ttfb is not None:
                delta_ttfb = float(cand_ttfb) - float(best_ttfb)
            ablation_rows.append(
                [
                    html.escape(target),
                    html.escape(best_cfg),
                    html.escape(cfg_name),
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
        target_aggs = [row for row in config_aggregates if row["target"] == target]
        if not target_aggs:
            continue
        best = max(target_aggs, key=lambda row: (row["mean_unique_bugs"] or -1.0, row["mean_interesting_tests"] or -1.0))
        best_cfg = str(best["config"])
        our_runs = runs_by_target_config.get(target, {}).get(best_cfg, [])

        baseline = _load_baseline_rows(baseline_dir=baseline_results_dir, target=target)
        selected_baseline = _select_best_baseline_run(rows=baseline)
        selected_baseline_rows = [{k: str(v) for k, v in row.items()} for row in selected_baseline]
        baseline_interesting = _compute_cumulative_metrics_over_iteration(rows=selected_baseline_rows, metric="interesting_tests")
        baseline_bugs = _compute_cumulative_metrics_over_iteration(rows=selected_baseline_rows, metric="unique_bugs")
        our_interesting_lines = [
            (f"our/{run.run_id}", _compute_cumulative_metrics_over_iteration(rows=run.rows, metric="interesting_tests"))
            for run in our_runs
        ]
        our_bug_lines = [
            (f"our/{run.run_id}", _compute_cumulative_metrics_over_iteration(rows=run.rows, metric="unique_bugs"))
            for run in our_runs
        ]

        fig_bugs = _plot_lines(
            title=f"RQ3 baseline unique bugs vs iteration — {target}",
            x_label="iteration",
            y_label="cumulative unique bugs",
            lines=our_bug_lines + [("AFL++ baseline", [(float(x), y) for x, y in baseline_bugs])],
            extend_to_chart_end=True,
        )
        out_bugs = charts_dir / f"rq3_baseline_unique_bugs_vs_time_{_slug(target)}.png"
        save_chart_png(fig_bugs, out_bugs)
        parts.append(_render_chart_html(title=f"Baseline unique bugs comparison ({target})", output_path=out_bugs, root=report_root))

        our_best_run = max((m for m in run_metrics if m["target"] == target and m["config"] == best_cfg), key=lambda x: int(x["total_unique_bugs"]), default=None)
        baseline_rows.append(
            [
                html.escape(target),
                "our_fuzzer",
                html.escape(best_cfg),
                html.escape(_to_str(best["run_count"])),
                html.escape(_to_str(best["mean_unique_bugs"])),
                html.escape(_to_str(best["mean_interesting_tests"])),
                html.escape(_to_str(best["mean_time_to_first_bug_seconds"])),
                html.escape(str(our_best_run["run_id"]) if our_best_run else "Data unavailable"),
            ]
        )
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
                            if float(row.get("isinteresting_score", 0) or 0)
                            > INTERESTING_SCORE_THRESHOLD
                        )
                    )
                ),
                "Data unavailable",
                "baseline.json",
            ]
        )
        parts.append(
            f"<p class='meta'>Target <code>{html.escape(target)}</code>: best internal config is "
            f"<code>{html.escape(best_cfg)}</code>. Baseline curves use iteration as x-axis as requested.</p>"
        )

    parts.append(
        _render_table(
            headers=[
                "target",
                "system",
                "config",
                "runs",
                "avg_unique_bugs",
                "avg_interesting_tests",
                "avg_time_to_first_bug_s",
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
    stability_rows: list[list[str]] = []
    for target, rows in sorted(by_target.items()):
        labels = [f"{row['config']}/{row['run_id']}" for row in rows]
        uniq = [float(row["total_unique_bugs"]) for row in rows]
        interesting = [float(row["total_interesting_tests"]) for row in rows]
        fig = _plot_grouped_bar(
            title=f"RQ4 per-run stability — {target}",
            categories=labels,
            series=[("unique_bugs", uniq), ("interesting_tests", interesting)],
        )
        out = charts_dir / f"rq4_stability_per_run_{_slug(target)}.png"
        save_chart_png(fig, out)
        parts.append(_render_chart_html(title=f"Per-run stability ({target})", output_path=out, root=report_root))

        uniq_stats = _summary_stats(uniq)
        int_stats = _summary_stats(interesting)
        stability_rows.append(
            [
                html.escape(target),
                html.escape(str(len(rows))),
                html.escape(_to_str(uniq_stats["mean"])),
                html.escape(_to_str(uniq_stats["std"])),
                html.escape(_to_str(uniq_stats["cv"])),
                html.escape(_to_str(uniq_stats["min"])),
                html.escape(_to_str(uniq_stats["max"])),
                html.escape(_to_str(int_stats["mean"])),
                html.escape(_to_str(int_stats["std"])),
                html.escape(_to_str(int_stats["cv"])),
            ]
        )
        if len(rows) == 1:
            parts.append(f"<p class='meta'>Target <code>{html.escape(target)}</code>: only one run available, stability cannot be estimated robustly.</p>")
        else:
            parts.append(f"<p class='meta'>Target <code>{html.escape(target)}</code>: run-to-run variance is reported via std/cv.</p>")
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
                "mean_interesting_tests",
                "std_interesting_tests",
                "cv_interesting_tests",
            ],
            rows=stability_rows,
            table_id="rq4-stability-summary",
        )
    )
    parts.append("</section>")
    return "".join(parts)


def generate_batch_report(*, batch_folder: Path) -> Path | None:
    run_folders = _find_run_folders(batch_folder=batch_folder)
    runs = [r for p in run_folders if (r := _load_run_data(run_folder=p)) is not None]
    if not runs:
        return None

    run_metrics = [compute_run_metrics(run=run) for run in runs]
    config_aggregates = compute_config_aggregates(run_metrics=run_metrics)
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
                "run_metrics": run_metrics,
                "config_aggregates": config_aggregates,
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
    rq1 = render_rq1_effectiveness(
        runs_by_target_config=runs_by_target_config,
        run_metrics=run_metrics,
        charts_dir=charts_dir,
        report_root=batch_folder,
    )
    rq2 = render_rq2_efficiency(
        run_metrics=run_metrics,
        config_aggregates=config_aggregates,
        charts_dir=charts_dir,
        report_root=batch_folder,
    )
    rq3 = render_rq3_baseline_ablation(
        runs_by_target_config=runs_by_target_config,
        run_metrics=run_metrics,
        config_aggregates=config_aggregates,
        charts_dir=charts_dir,
        report_root=batch_folder,
        baseline_results_dir=baseline_results_dir,
    )
    rq4 = render_rq4_stability(
        run_metrics=run_metrics,
        charts_dir=charts_dir,
        report_root=batch_folder,
    )

    toc_links = [
        ("Comparison Overview", "comparison-overview"),
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

