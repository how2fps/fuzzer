from __future__ import annotations

import csv
import html
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RunMetrics:
    run_folder: Path
    config_label: str
    target: str
    total_iterations: int
    bug_iterations: int
    unique_bug_signatures: int
    avg_isinteresting_score: float | None
    first_created_at: str | None
    last_created_at: str | None


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


def _parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _find_run_folders(*, batch_folder: Path) -> list[Path]:
    out: list[Path] = []
    if not batch_folder.is_dir():
        return out
    for p in batch_folder.rglob("runs.csv"):
        out.append(p.parent)
    return sorted(set(out))


def _load_run_metrics(*, run_folder: Path, batch_folder: Path) -> RunMetrics | None:
    runs_csv = run_folder / "runs.csv"
    if not runs_csv.is_file():
        return None

    config_label = run_folder.parent.name

    total_iterations = 0
    bug_iterations = 0
    signatures: set[tuple[str | None, str | None, str | None, str | None]] = set()
    scores: list[float] = []
    created_at_values: list[datetime] = []
    targets: set[str] = set()

    with open(runs_csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            total_iterations += 1
            status = (row.get("status") or "").strip().lower()
            if status in {"bug", "crash", "timeout"}:
                bug_iterations += 1
                signatures.add(
                    (
                        row.get("bug_type") or None,
                        row.get("exception") or None,
                        row.get("file") or None,
                        str(row.get("line") or "") or None,
                    )
                )

            score = _safe_float(row.get("isinteresting_score"))
            if score is not None:
                scores.append(score)

            created_at = _parse_iso8601(row.get("created_at"))
            if created_at is not None:
                created_at_values.append(created_at)

            target = (row.get("target") or "").strip()
            if target:
                targets.add(target)

    avg_score = (sum(scores) / len(scores)) if scores else None
    created_at_values.sort()
    first_created_at = created_at_values[0].isoformat() if created_at_values else None
    last_created_at = created_at_values[-1].isoformat() if created_at_values else None
    target_value = ",".join(sorted(targets)) if targets else "unknown"

    return RunMetrics(
        run_folder=run_folder,
        config_label=config_label,
        target=target_value,
        total_iterations=total_iterations,
        bug_iterations=bug_iterations,
        unique_bug_signatures=len(signatures),
        avg_isinteresting_score=avg_score,
        first_created_at=first_created_at,
        last_created_at=last_created_at,
    )


def _svg_bar_chart(*, title: str, rows: list[tuple[str, float]], width: int = 900, height: int = 260) -> str:
    if not rows:
        return f"<h3>{html.escape(title)}</h3><p>No data.</p>"

    max_value = max(v for _, v in rows) if rows else 1.0
    max_value = max(max_value, 1e-9)

    pad_left = 220
    pad_right = 20
    pad_top = 40
    pad_bottom = 20
    chart_w = width - pad_left - pad_right
    chart_h = height - pad_top - pad_bottom
    bar_gap = 8
    bar_h = max(10, int((chart_h - bar_gap * (len(rows) - 1)) / max(1, len(rows))))

    parts: list[str] = []
    parts.append(f"<h3>{html.escape(title)}</h3>")
    parts.append(
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" role="img" aria-label="{html.escape(title)}">'
    )
    parts.append(f"<rect x='0' y='0' width='{width}' height='{height}' fill='#0b0f14' rx='14'/>")
    parts.append(f"<text x='{pad_left}' y='26' fill='#e6edf3' font-size='16' font-family='ui-monospace, SFMono-Regular, Menlo, Consolas, monospace'>{html.escape(title)}</text>")

    y = pad_top
    for label, value in rows:
        ratio = max(0.0, min(1.0, value / max_value))
        w = int(chart_w * ratio)
        parts.append(f"<text x='18' y='{y + bar_h - 3}' fill='#9fb2c6' font-size='12' font-family='ui-monospace, SFMono-Regular, Menlo, Consolas, monospace'>{html.escape(label)}</text>")
        parts.append(f"<rect x='{pad_left}' y='{y}' width='{w}' height='{bar_h}' fill='#3fb950' opacity='0.9' rx='6'/>")
        parts.append(f"<text x='{pad_left + w + 8}' y='{y + bar_h - 3}' fill='#e6edf3' font-size='12' font-family='ui-monospace, SFMono-Regular, Menlo, Consolas, monospace'>{html.escape(f'{value:.3f}' if isinstance(value, float) else str(value))}</text>")
        y += bar_h + bar_gap

    parts.append("</svg>")
    return "\n".join(parts)


def generate_batch_report(*, batch_folder: Path) -> Path | None:
    """
    Scan a batch folder (results/batch_*) for run subfolders (containing runs.csv)
    and write an overview report.html with charts + tables into the batch folder.
    """
    run_folders = _find_run_folders(batch_folder=batch_folder)
    metrics: list[RunMetrics] = []
    for run_folder in run_folders:
        m = _load_run_metrics(run_folder=run_folder, batch_folder=batch_folder)
        if m is not None:
            metrics.append(m)

    if not metrics:
        return None

    def mean(values: list[float]) -> float | None:
        return (sum(values) / len(values)) if values else None

    def summarize_by_config(*, ms: list[RunMetrics]) -> list[dict[str, Any]]:
        by_config: dict[str, list[RunMetrics]] = {}
        for m in ms:
            by_config.setdefault(m.config_label, []).append(m)

        config_summary: list[dict[str, Any]] = []
        for config_label, cms in sorted(by_config.items()):
            total_iters = sum(x.total_iterations for x in cms)
            total_bug_iters = sum(x.bug_iterations for x in cms)
            total_unique_sigs = sum(x.unique_bug_signatures for x in cms)
            avg_scores = [x.avg_isinteresting_score for x in cms if x.avg_isinteresting_score is not None]
            config_summary.append(
                {
                    "config_label": config_label,
                    "runs": len(cms),
                    "total_iterations": total_iters,
                    "bug_iterations": total_bug_iters,
                    "bug_rate": (total_bug_iters / total_iters) if total_iters else 0.0,
                    "unique_bug_signatures_sum": total_unique_sigs,
                    "avg_isinteresting_score_mean": mean([float(x) for x in avg_scores]),
                }
            )
        return config_summary

    by_target: dict[str, list[RunMetrics]] = {}
    for m in metrics:
        by_target.setdefault(m.target, []).append(m)

    report_path = batch_folder / "report.html"
    data_path = batch_folder / "report.json"
    generated_at = datetime.utcnow().isoformat() + "Z"
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "batch_folder": str(batch_folder),
                "generated_at": generated_at,
                "targets": {
                    target: {
                        "config_summary": summarize_by_config(ms=sorted(ms, key=lambda x: x.config_label)),
                        "runs": [
                            {
                                "run_folder": str(m.run_folder),
                                "target": m.target,
                                "config_label": m.config_label,
                                "total_iterations": m.total_iterations,
                                "bug_iterations": m.bug_iterations,
                                "unique_bug_signatures": m.unique_bug_signatures,
                                "avg_isinteresting_score": m.avg_isinteresting_score,
                                "first_created_at": m.first_created_at,
                                "last_created_at": m.last_created_at,
                            }
                            for m in sorted(ms, key=lambda x: (x.config_label, str(x.run_folder)))
                        ],
                    }
                    for target, ms in sorted(by_target.items())
                },
            },
            f,
            indent=2,
        )

    def _render_config_table(*, config_summary: list[dict[str, Any]]) -> str:
        row_parts: list[str] = []
        for x in config_summary:
            avg_score = x["avg_isinteresting_score_mean"]
            avg_score_str = "" if avg_score is None else f"{float(avg_score):.4f}"
            row_parts.append(
                "<tr>"
                f"<td>{html.escape(str(x['config_label']))}</td>"
                f"<td>{int(x['runs'])}</td>"
                f"<td>{int(x['total_iterations'])}</td>"
                f"<td>{int(x['bug_iterations'])}</td>"
                f"<td>{float(x['bug_rate']):.4f}</td>"
                f"<td>{int(x['unique_bug_signatures_sum'])}</td>"
                f"<td>{html.escape(avg_score_str)}</td>"
                "</tr>"
            )
        rows_table = "\n".join(row_parts) if row_parts else ""
        return f"""
      <table>
        <thead>
          <tr>
            <th>config</th>
            <th>runs</th>
            <th>iterations</th>
            <th>bug iters</th>
            <th>bug rate</th>
            <th>unique bug sigs (sum)</th>
            <th>avg score (mean)</th>
          </tr>
        </thead>
        <tbody>
          {rows_table}
        </tbody>
      </table>
"""

    def _slug(s: str) -> str:
        safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in s).strip("-")
        while "--" in safe:
            safe = safe.replace("--", "-")
        return safe or "target"

    target_sections: list[str] = []
    toc_links: list[str] = []
    for target, ms in sorted(by_target.items()):
        config_summary = summarize_by_config(ms=ms)
        chart_bug_rate = [(x["config_label"], float(x["bug_rate"])) for x in config_summary]
        chart_unique = [(x["config_label"], float(x["unique_bug_signatures_sum"])) for x in config_summary]
        chart_iters = [(x["config_label"], float(x["total_iterations"])) for x in config_summary]

        anchor = _slug(target)
        toc_links.append(f"<a href='#{html.escape(anchor)}'>{html.escape(target)}</a>")
        target_sections.append(
            f"""
      <h2 id="{html.escape(anchor)}">Target: <code>{html.escape(target)}</code></h2>
      <div class="meta">Runs: <code>{len(ms)}</code></div>
      <h3>Config summary</h3>
      {_render_config_table(config_summary=config_summary)}
      <h3>Charts (higher isn’t always “better”)</h3>
      {_svg_bar_chart(title="Bug rate (bug/crash/timeout iterations / total iterations)", rows=chart_bug_rate)}
      {_svg_bar_chart(title="Unique bug signatures (sum across runs)", rows=chart_unique)}
      {_svg_bar_chart(title="Total iterations (sum across runs)", rows=chart_iters)}
"""
        )
    toc_html = " · ".join(toc_links)
    targets_html = "\n".join(target_sections)

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
      --border: rgba(255,255,255,0.08);
    }}
    body {{
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Arial, sans-serif;
      background: radial-gradient(1200px 800px at 20% 0%, #0f1722, var(--bg));
      color: var(--text);
    }}
    .wrap {{ max-width: 1100px; margin: 0 auto; padding: 28px 18px 60px; }}
    .card {{
      background: rgba(11,15,20,0.85);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 16px 16px;
      box-shadow: 0 20px 40px rgba(0,0,0,0.35);
      backdrop-filter: blur(6px);
    }}
    h1 {{ margin: 0 0 6px; font-size: 22px; }}
    .meta {{ color: var(--muted); font-size: 13px; margin-bottom: 12px; }}
    h2 {{ margin: 20px 0 10px; font-size: 16px; color: var(--text); }}
    h3 {{ margin: 18px 0 8px; font-size: 14px; color: var(--text); }}
    .toc {{ margin: 10px 0 14px; font-size: 13px; color: var(--muted); }}
    .toc a {{ color: #79c0ff; text-decoration: none; }}
    .toc a:hover {{ text-decoration: underline; }}
    table {{ width: 100%; border-collapse: collapse; overflow: hidden; border-radius: 12px; }}
    th, td {{ padding: 10px 10px; border-bottom: 1px solid var(--border); font-size: 13px; }}
    th {{ text-align: left; color: var(--muted); font-weight: 600; }}
    tr:hover td {{ background: rgba(255,255,255,0.03); }}
    code {{ color: #d2a8ff; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>Fuzzer batch report</h1>
      <div class="meta">
        Batch folder: <code>{html.escape(str(batch_folder))}</code><br/>
        Generated: <code>{html.escape(generated_at)}</code><br/>
        Raw data: <code>report.json</code>
      </div>

      <div class="toc">
        Targets: {toc_html}
      </div>
{targets_html}
    </div>
  </div>
</body>
</html>
"""

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html_out)

    return report_path

