#!/usr/bin/env python3
"""
Re-run results export (runs.csv, unique_error_line_pairs; does not copy bug_counts) and
batch report (report.html / report.json) for an existing results tree.

Usage:
  python rerun_results_export.py results/batch_20260322_093204
  python rerun_results_export.py results/batch_20260322_093204/json_heap/run_1_20260322_083245

Auto-detect:
  - Path contains runs.db at that level → export that run only; if a parent folder
    is named batch_*, also regenerate that batch's report.
  - Otherwise → treat path as a batch root: export every run with runs.db under it,
    then generate_batch_report for that path.

  --report-root DIR   Always run generate_batch_report(batch_folder=DIR) after exports.
  --no-report         Skip generate_batch_report entirely.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import html
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import core.batch_report as batch_report
from core.fuzzer_logging import configure_fuzzer_logging, get_fuzzer_logger
from core.results_export import export_results

RERUN_REPORT_INTERESTING_SCORE_THRESHOLD = 0.6
RQ3_COMBINED_PARSER_TARGET = "ipv4-ipv6-parser"
RQ3_COMBINED_PARSER_SOURCE_TARGETS = {"ipv4-parser", "ipv6-parser"}
RQ3_COMBINED_PARSER_ALIASES = {
    RQ3_COMBINED_PARSER_TARGET,
    "IPv4-IPv6-parser",
}


@dataclass(frozen=True)
class Rq3SyntheticEntry:
    target: str
    config: str
    run_label: str
    total_unique_bugs: int
    total_interesting_tests: int
    time_to_first_bug_seconds: float | None
    bug_curve: list[tuple[int, int]]


def _target_for_run_folder(run_folder: Path, db_path: Path) -> str:
    cfg = run_folder / "config.json"
    if cfg.is_file():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            t = data.get("target")
            if isinstance(t, str) and t.strip():
                return t.strip()
        except (json.JSONDecodeError, OSError):
            pass
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT target FROM runs LIMIT 1").fetchone()
            if row and row[0]:
                return str(row[0])
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    return "unknown"


def _find_batch_ancestor(path: Path) -> Path | None:
    for parent in [path, *path.parents]:
        if parent.name.startswith("batch_"):
            return parent
    return None


def _export_one_run(run_folder: Path, log) -> bool:
    db_path = run_folder / "runs.db"
    if not db_path.is_file():
        return False
    target = _target_for_run_folder(run_folder, db_path)
    log.info("Exporting run folder %s (target=%s)", run_folder, target)
    export_results(
        results_folder=run_folder,
        db_path=db_path,
        target=target,
        copy_bug_counts=False,
    )
    return True


def _export_all_runs_under(root: Path, log) -> int:
    roots = sorted({p.parent for p in root.rglob("runs.db") if p.is_file()})
    n = 0
    for run_folder in roots:
        if _export_one_run(run_folder, log):
            n += 1
    return n


def _is_parser_source_target(target: str) -> bool:
    return target in RQ3_COMBINED_PARSER_SOURCE_TARGETS


def _is_parser_combined_target(target: str) -> bool:
    return target in RQ3_COMBINED_PARSER_ALIASES


def _is_any_parser_target(target: str) -> bool:
    return _is_parser_source_target(target) or _is_parser_combined_target(target)


def _best_metric_sort_key(metric: dict[str, object]) -> tuple[int, int]:
    return (
        int(metric["total_unique_bugs"]),
        int(metric["total_interesting_tests"]),
    )


def _namespaced_bug_signature(run: batch_report.RunData, row: dict[str, str]) -> str:
    return f"{run.target}|{batch_report.normalize_bug_signature(row)}"


def _compute_namespaced_bug_curve(run: batch_report.RunData) -> list[tuple[int, int]]:
    rows = batch_report._unique_bug_metric_rows(run=run)
    if not rows:
        return []
    ordered = sorted(rows, key=lambda row: batch_report._safe_int(row.get("iteration")) or 0)
    last_iteration = max((batch_report._safe_int(row.get("iteration")) or 0 for row in ordered), default=0)
    points: list[tuple[int, int]] = [(0, 0)]
    seen_bugs: set[str] = set()
    count = 0
    for row in ordered:
        if (row.get("status") or "").strip().lower() not in batch_report.BUG_STATUSES:
            continue
        sig = _namespaced_bug_signature(run, row)
        if sig in seen_bugs:
            continue
        seen_bugs.add(sig)
        count = len(seen_bugs)
        points.append((batch_report._safe_int(row.get("iteration")) or 0, count))
    if points[-1][0] < last_iteration:
        points.append((last_iteration, count))
    return points


def _sum_step_curves(curves: list[list[tuple[int, int]]]) -> list[tuple[int, int]]:
    non_empty = [curve for curve in curves if curve]
    if not non_empty:
        return []
    xs = sorted({int(x) for curve in non_empty for x, _ in curve})
    result: list[tuple[int, int]] = []
    for x in xs:
        total = 0
        for curve in non_empty:
            value = 0
            for px, py in curve:
                if int(px) <= x:
                    value = int(py)
                else:
                    break
            total += value
        result.append((x, total))
    return result


def _parser_group_key(config_name: str) -> str:
    pattern = r"^(\d+_)?(?:ipv4-parser|ipv6-parser|IPv4-IPv6-parser|ipv4-ipv6-parser)([_-])"
    return re.sub(pattern, lambda m: f"{m.group(1) or ''}parser{m.group(2)}", config_name, count=1)


def _parser_baseline_rows() -> list[dict[str, object]]:
    baseline_dir = Path("baseline_results")
    for alias in ("parser", "IPv4-IPv6-parser", "ipv4-ipv6-parser"):
        rows = batch_report._load_baseline_rows(baseline_dir=baseline_dir, target=alias)
        if rows:
            return rows
    return []


def _build_synthetic_entry_from_runs(
    *,
    target: str,
    config_label: str,
    selected_runs: list[batch_report.RunData],
) -> Rq3SyntheticEntry:
    metrics = [
        batch_report.compute_run_metrics(
            run=run,
            interesting_score_threshold=RERUN_REPORT_INTERESTING_SCORE_THRESHOLD,
        )
        for run in selected_runs
    ]
    bug_signatures: set[str] = set()
    for run in selected_runs:
        for row in batch_report._unique_bug_metric_rows(run=run):
            if (row.get("status") or "").strip().lower() not in batch_report.BUG_STATUSES:
                continue
            bug_signatures.add(_namespaced_bug_signature(run, row))
    interesting_tests = sum(int(metric["total_interesting_tests"]) for metric in metrics)
    ttfb_values = [
        float(metric["time_to_first_bug_seconds"])
        for metric in metrics
        if metric["time_to_first_bug_seconds"] is not None
    ]
    bug_curve = _sum_step_curves([_compute_namespaced_bug_curve(run) for run in selected_runs])
    run_label = " + ".join(f"{run.config_name}/{run.run_id}" for run in selected_runs)
    return Rq3SyntheticEntry(
        target=target,
        config=config_label,
        run_label=run_label,
        total_unique_bugs=len(bug_signatures),
        total_interesting_tests=interesting_tests,
        time_to_first_bug_seconds=min(ttfb_values) if ttfb_values else None,
        bug_curve=bug_curve,
    )


def _best_parser_entry_from_runs(runs: list[batch_report.RunData]) -> Rq3SyntheticEntry | None:
    parser_runs = [run for run in runs if _is_any_parser_target(run.target)]
    if not parser_runs:
        return None
    grouped: dict[str, list[batch_report.RunData]] = {}
    for run in parser_runs:
        grouped.setdefault(_parser_group_key(run.config_name), []).append(run)

    candidates: list[Rq3SyntheticEntry] = []
    for config_key, group_runs in grouped.items():
        combined_runs = [run for run in group_runs if _is_parser_combined_target(run.target)]
        if combined_runs:
            best_combined = max(
                combined_runs,
                key=lambda run: _best_metric_sort_key(
                    batch_report.compute_run_metrics(
                        run=run,
                        interesting_score_threshold=RERUN_REPORT_INTERESTING_SCORE_THRESHOLD,
                    )
                ),
            )
            candidates.append(
                _build_synthetic_entry_from_runs(
                    target=RQ3_COMBINED_PARSER_TARGET,
                    config_label=config_key,
                    selected_runs=[best_combined],
                )
            )

        selected_component_runs: list[batch_report.RunData] = []
        for source_target in sorted(RQ3_COMBINED_PARSER_SOURCE_TARGETS):
            source_runs = [run for run in group_runs if run.target == source_target]
            if not source_runs:
                continue
            best_source = max(
                source_runs,
                key=lambda run: _best_metric_sort_key(
                    batch_report.compute_run_metrics(
                        run=run,
                        interesting_score_threshold=RERUN_REPORT_INTERESTING_SCORE_THRESHOLD,
                    )
                ),
            )
            selected_component_runs.append(best_source)
        if selected_component_runs:
            candidates.append(
                _build_synthetic_entry_from_runs(
                    target=RQ3_COMBINED_PARSER_TARGET,
                    config_label=config_key,
                    selected_runs=selected_component_runs,
                )
            )

    if not candidates:
        return None
    return max(
        candidates,
        key=lambda entry: (entry.total_unique_bugs, entry.total_interesting_tests),
    )


def _render_rq3_with_combined_parsers(report_target: Path) -> str | None:
    runs = batch_report._load_runs_from_batch_folder(batch_folder=report_target)
    if not runs:
        return None

    run_metrics = [
        batch_report.compute_run_metrics(
            run=run,
            interesting_score_threshold=RERUN_REPORT_INTERESTING_SCORE_THRESHOLD,
        )
        for run in runs
    ]
    run_data_by_key = {
        (run.target, run.config_name, run.run_id): run
        for run in runs
    }
    current_label = report_target.name
    charts_dir = report_target / "output" / "charts"
    baseline_results_dir = Path("baseline_results")
    checkpoint_batches = [
        checkpoint_dir
        for checkpoint_dir in batch_report._discover_checkpoint_batches(batch_folder=report_target)
        if checkpoint_dir.resolve() != report_target.resolve()
    ]
    checkpoint_best_by_folder: dict[str, tuple[dict[str, batch_report.RunData], dict[str, dict[str, object]]]] = {}
    checkpoint_parser_entries: dict[str, Rq3SyntheticEntry] = {}
    for checkpoint_dir in checkpoint_batches:
        checkpoint_runs = batch_report._load_runs_from_batch_folder(batch_folder=checkpoint_dir)
        if not checkpoint_runs:
            continue
        checkpoint_best_by_folder[checkpoint_dir.name] = batch_report._best_run_metric_by_target(
            runs=checkpoint_runs,
            interesting_score_threshold=RERUN_REPORT_INTERESTING_SCORE_THRESHOLD,
        )
        parser_entry = _best_parser_entry_from_runs(checkpoint_runs)
        if parser_entry is not None:
            checkpoint_parser_entries[checkpoint_dir.name] = parser_entry

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
    non_parser_targets = sorted({str(row["target"]) for row in run_metrics if not _is_any_parser_target(str(row["target"]))})
    for target in non_parser_targets:
        target_metrics = [row for row in run_metrics if row["target"] == target]
        if not target_metrics:
            continue
        best_run_metric = max(target_metrics, key=_best_metric_sort_key)
        best_cfg = str(best_run_metric["config"])
        best_run_id = str(best_run_metric["run_id"])
        best_run_data = run_data_by_key.get((target, best_cfg, best_run_id))
        best_config_payload = best_run_data.config if best_run_data is not None else {}
        candidate_best_by_config: dict[str, dict[str, object]] = {}
        for metric in target_metrics:
            cfg_name = str(metric["config"])
            previous = candidate_best_by_config.get(cfg_name)
            if previous is None or _best_metric_sort_key(metric) > _best_metric_sort_key(previous):
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
                    html.escape(batch_report._to_str(delta_bugs)),
                    html.escape(batch_report._to_str(delta_interest)),
                    html.escape(batch_report._to_str(delta_ttfb)),
                ]
            )
            impact_rows.append((changed_modules[0], abs(delta_bugs) + abs(delta_interest)))

    parts.append(
        batch_report._render_table(
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
        ranking_rows = [
            [html.escape(label), html.escape(f"{value:.3f}")]
            for label, value in sorted(impacts.items(), key=lambda item: item[1], reverse=True)
        ]
        parts.append("<h4>Module impact ranking</h4>")
        parts.append(
            batch_report._render_table(
                headers=["module", "impact_score"],
                rows=ranking_rows,
                table_id="rq3-impact",
            )
        )

    parts.append("<h3>Part B: Baseline comparison</h3>")
    baseline_rows: list[list[str]] = []
    targets_for_baseline = non_parser_targets.copy()
    parser_entry = _best_parser_entry_from_runs(runs)
    if parser_entry is not None:
        targets_for_baseline.append(RQ3_COMBINED_PARSER_TARGET)

    for target in sorted(targets_for_baseline):
        if target == RQ3_COMBINED_PARSER_TARGET:
            assert parser_entry is not None
            baseline = _parser_baseline_rows()
            selected_baseline = batch_report._select_best_baseline_run(rows=baseline)
            selected_baseline_rows = [{k: str(v) for k, v in row.items()} for row in selected_baseline]
            baseline_bugs = batch_report._compute_cumulative_metrics_over_iteration(
                rows=selected_baseline_rows,
                metric="unique_bugs",
            )
            checkpoint_bug_lines: list[tuple[str, list[tuple[int, int]]]] = []
            checkpoint_summary_rows: list[list[str]] = []
            for checkpoint_name in sorted(checkpoint_parser_entries.keys()):
                checkpoint_entry = checkpoint_parser_entries[checkpoint_name]
                checkpoint_bug_lines.append((checkpoint_name, checkpoint_entry.bug_curve))
                checkpoint_summary_rows.append(
                    [
                        html.escape(target),
                        html.escape(checkpoint_name),
                        html.escape(checkpoint_entry.config),
                        "1",
                        html.escape(str(checkpoint_entry.total_unique_bugs)),
                        html.escape(str(checkpoint_entry.total_interesting_tests)),
                        html.escape(batch_report._to_str(checkpoint_entry.time_to_first_bug_seconds)),
                        html.escape(checkpoint_entry.run_label),
                    ]
                )

            fig_bugs = batch_report._plot_lines(
                title=f"RQ3 baseline unique bugs vs iteration — {target}",
                x_label="iteration",
                y_label="cumulative unique bugs",
                lines=[(current_label, parser_entry.bug_curve)] + checkpoint_bug_lines + [("AFL++ baseline", [(int(x), y) for x, y in baseline_bugs])],
                extend_to_chart_end=True,
            )
            out_bugs = charts_dir / f"rq3_baseline_unique_bugs_vs_time_{batch_report._slug(target)}.png"
            batch_report.save_chart_png(fig_bugs, out_bugs)
            parts.append(
                batch_report._render_chart_html(
                    title=f"Baseline unique bugs comparison ({target})",
                    output_path=out_bugs,
                    root=report_target,
                )
            )

            baseline_rows.append(
                [
                    html.escape(target),
                    html.escape(current_label),
                    html.escape(parser_entry.config),
                    "1",
                    html.escape(str(parser_entry.total_unique_bugs)),
                    html.escape(str(parser_entry.total_interesting_tests)),
                    html.escape(batch_report._to_str(parser_entry.time_to_first_bug_seconds)),
                    html.escape(parser_entry.run_label),
                ]
            )
            baseline_rows.extend(checkpoint_summary_rows)
            baseline_rows.append(
                [
                    html.escape(target),
                    "afl++_blackbox",
                    "baseline",
                    "1",
                    html.escape(str(batch_report._baseline_run_unique_bug_count(rows=selected_baseline))),
                    html.escape(
                        str(
                            sum(
                                1
                                for row in selected_baseline
                                if batch_report._is_interesting_score(
                                    score=batch_report._safe_float(row.get("isinteresting_score")),
                                    threshold=RERUN_REPORT_INTERESTING_SCORE_THRESHOLD,
                                )
                            )
                        )
                    ),
                    "Data unavailable",
                    "baseline.json",
                ]
            )
            parts.append(
                f"<p class='meta'>Target <code>{html.escape(target)}</code>: RQ3 combines the best parser config by "
                "unioning target-scoped unique bugs from the selected IPv4 and IPv6 runs, then compares that merged curve "
                f"against sibling <code>checkpoint_*</code> folders under <code>{html.escape(str(report_target.parent))}</code>. "
                "Baseline curves use iteration as x-axis as requested.</p>"
            )
            continue

        target_metrics = [row for row in run_metrics if row["target"] == target]
        if not target_metrics:
            continue
        best_run_metric = max(target_metrics, key=_best_metric_sort_key)
        best_cfg = str(best_run_metric["config"])
        best_run_id = str(best_run_metric["run_id"])
        our_best_run_data = run_data_by_key.get((target, best_cfg, best_run_id))
        baseline = batch_report._load_baseline_rows(baseline_dir=baseline_results_dir, target=target)
        selected_baseline = batch_report._select_best_baseline_run(rows=baseline)
        selected_baseline_rows = [{k: str(v) for k, v in row.items()} for row in selected_baseline]
        baseline_bugs = batch_report._compute_cumulative_metrics_over_iteration(rows=selected_baseline_rows, metric="unique_bugs")
        our_bug_lines = []
        if our_best_run_data is not None:
            our_bug_lines.append(
                (
                    current_label,
                    batch_report._compute_cumulative_metrics_over_iteration(
                        rows=batch_report._unique_bug_metric_rows(run=our_best_run_data),
                        metric="unique_bugs",
                    ),
                )
            )

        checkpoint_bug_lines = []
        checkpoint_summary_rows = []
        for checkpoint_name in sorted(checkpoint_best_by_folder.keys()):
            checkpoint_run_data_by_target, checkpoint_metrics_by_target = checkpoint_best_by_folder[checkpoint_name]
            checkpoint_metric = checkpoint_metrics_by_target.get(target)
            checkpoint_run_data = checkpoint_run_data_by_target.get(target)
            if checkpoint_metric is None or checkpoint_run_data is None:
                continue
            checkpoint_bug_lines.append(
                (
                    checkpoint_name,
                    batch_report._compute_cumulative_metrics_over_iteration(
                        rows=batch_report._unique_bug_metric_rows(run=checkpoint_run_data),
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
                    html.escape(batch_report._to_str(checkpoint_metric["total_unique_bugs"])),
                    html.escape(batch_report._to_str(checkpoint_metric["total_interesting_tests"])),
                    html.escape(batch_report._to_str(checkpoint_metric["time_to_first_bug_seconds"])),
                    html.escape(str(checkpoint_metric["run_id"])),
                ]
            )

        fig_bugs = batch_report._plot_lines(
            title=f"RQ3 baseline unique bugs vs iteration — {target}",
            x_label="iteration",
            y_label="cumulative unique bugs",
            lines=our_bug_lines + checkpoint_bug_lines + [("AFL++ baseline", [(int(x), y) for x, y in baseline_bugs])],
            extend_to_chart_end=True,
        )
        out_bugs = charts_dir / f"rq3_baseline_unique_bugs_vs_time_{batch_report._slug(target)}.png"
        batch_report.save_chart_png(fig_bugs, out_bugs)
        parts.append(
            batch_report._render_chart_html(
                title=f"Baseline unique bugs comparison ({target})",
                output_path=out_bugs,
                root=report_target,
            )
        )

        baseline_rows.append(
            [
                html.escape(target),
                html.escape(current_label),
                html.escape(best_cfg),
                "1",
                html.escape(batch_report._to_str(best_run_metric["total_unique_bugs"])),
                html.escape(batch_report._to_str(best_run_metric["total_interesting_tests"])),
                html.escape(batch_report._to_str(best_run_metric["time_to_first_bug_seconds"])),
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
                html.escape(str(batch_report._baseline_run_unique_bug_count(rows=selected_baseline))),
                html.escape(
                    str(
                        sum(
                            1
                            for row in selected_baseline
                            if batch_report._is_interesting_score(
                                score=batch_report._safe_float(row.get("isinteresting_score")),
                                threshold=RERUN_REPORT_INTERESTING_SCORE_THRESHOLD,
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
            f"Comparison includes all sibling <code>checkpoint_*</code> result folders under <code>{html.escape(str(report_target.parent))}</code>. "
            "Baseline curves use iteration as x-axis as requested.</p>"
        )

    parts.append(
        batch_report._render_table(
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


def _rewrite_rq3_section(report_target: Path, log) -> None:
    report_path = report_target / "report.html"
    if not report_path.is_file():
        log.warning("Skipping RQ3 rewrite; report file not found: %s", report_path)
        return

    rq3_html = _render_rq3_with_combined_parsers(report_target)
    if not rq3_html:
        log.warning("Skipping RQ3 rewrite; no runs.csv found under %s", report_target)
        return

    try:
        html = report_path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("Skipping RQ3 rewrite; could not read %s: %s", report_path, exc)
        return

    updated_html, replacements = re.subn(
        r"<section id='rq3'>.*?</section>",
        rq3_html,
        html,
        count=1,
        flags=re.DOTALL,
    )
    if replacements != 1:
        log.warning("Skipping RQ3 rewrite; could not locate rq3 section in %s", report_path)
        return

    try:
        report_path.write_text(updated_html, encoding="utf-8")
    except OSError as exc:
        log.warning("Skipping RQ3 rewrite; could not write %s: %s", report_path, exc)
        return

    log.info(
        "Rewrote RQ3 in %s with combined parser target %s",
        report_path,
        RQ3_COMBINED_PARSER_TARGET,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "folder",
        type=Path,
        help="Batch results directory or a single run directory (contains runs.db)",
    )
    parser.add_argument(
        "--report-root",
        type=Path,
        default=None,
        help="After exports, regenerate report.html for this batch folder",
    )
    parser.add_argument(
        "--no-report",
        action="store_true",
        help="Do not run generate_batch_report",
    )
    args = parser.parse_args()
    folder = args.folder.expanduser().resolve()
    if not folder.is_dir():
        print(f"Not a directory: {folder}", file=sys.stderr)
        return 2

    configure_fuzzer_logging()
    log = get_fuzzer_logger()

    report_target: Path | None = None
    if not args.no_report:
        if args.report_root is not None:
            report_target = args.report_root.expanduser().resolve()
        elif (folder / "runs.db").is_file():
            report_target = _find_batch_ancestor(folder)
        else:
            report_target = folder

    if (folder / "runs.db").is_file():
        if not _export_one_run(folder, log):
            return 1
        if report_target is not None and not report_target.is_dir():
            log.warning("Report root is not a directory: %s", report_target)
            report_target = None
    else:
        count = _export_all_runs_under(folder, log)
        if count == 0:
            log.error("No runs.db found under %s", folder)
            return 1

    if args.no_report:
        return 0

    if report_target is None:
        log.info("No batch report root (use --report-root or a path under batch_*).")
        return 0

    log.info(
        "Generating batch report for %s (interestingness threshold=%s)",
        report_target,
        RERUN_REPORT_INTERESTING_SCORE_THRESHOLD,
    )
    out = batch_report.generate_batch_report(
        batch_folder=report_target,
        interesting_score_threshold=RERUN_REPORT_INTERESTING_SCORE_THRESHOLD,
    )
    if out is None:
        log.warning("Batch report not written (no runs.csv found under %s).", report_target)
        return 1
    _rewrite_rq3_section(report_target, log)
    log.info("Wrote %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
