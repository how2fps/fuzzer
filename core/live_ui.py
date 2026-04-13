from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console(
    stderr=True,
    force_terminal=True if sys.stderr.isatty() else None,
    force_interactive=True if sys.stderr.isatty() else None,
)


def _fmt_bool(value: bool) -> str:
    return "yes" if value else "no"


def _fmt_limit(config: Mapping[str, object]) -> str:
    if config["max_hours"] is not None:
        return f"{config['max_hours']}h"
    return str(config["max_iterations"])


def render_config_panel(
    *,
    config: Mapping[str, object],
    config_label: str,
    run_index: int,
    total_runs: int,
) -> Panel:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold cyan", justify="right")
    table.add_column(style="white")

    rows = [
        ("config", config_label),
        ("run", f"{run_index}/{total_runs}"),
        ("target", config["target"]),
        ("scheduler", config["scheduler_kind"]),
        ("mutator", config["mutator_kind"]),
        ("workers", str(config["workers"])),
        ("mem telemetry", f"{config['memory_telemetry_seconds']}s"),
        ("worker recycle", str(config.get("worker_max_jobs", 0))),
        ("limit", _fmt_limit(config)),
        ("timeout", f"{config['timeout']}s"),
        ("seed", str(config["rng_seed"])),
        ("parser", config["parser_version"]),
        ("interestingness", config["isinteresting_version"]),
        ("power scheduler", config["power_scheduler_version"]),
        ("seed corpus", config["seed_corpus_version"]),
    ]
    grammar_rules_file = config.get("grammar_rules_file")
    if grammar_rules_file:
        rows.append(("grammar rules", str(grammar_rules_file)))
    ast_grammar_path = config.get("ast_grammar_path")
    if ast_grammar_path:
        rows.append(("ast grammar", str(ast_grammar_path)))
    for label, value in rows:
        table.add_row(label, value)

    return Panel(table, title="Fuzzer Configuration", border_style="cyan")


def render_run_summary_panel(
    *,
    target: str,
    results_folder: str,
    summary: Mapping[str, object],
) -> Panel:
    stats = Table.grid(expand=True)
    for _ in range(6):
        stats.add_column(justify="center")
    status_counts = summary.get("status_counts")
    if not isinstance(status_counts, Mapping):
        status_counts = {}
    stats.add_row(
        Text.assemble(("Target\n", "dim"), str(target)),
        Text.assemble(("Results\n", "dim"), str(summary.get("total_results", 0))),
        Text.assemble(("Interesting\n", "dim"), str(summary.get("interesting_results", 0))),
        Text.assemble(("Crashes\n", "dim"), str(status_counts.get("crash", 0))),
        Text.assemble(("Unique Bugs\n", "dim"), str(summary.get("unique_bug_count", 0))),
        Text.assemble(("Timeouts\n", "dim"), str(status_counts.get("timeout", 0))),
    )

    bug_table = Table(
        expand=True,
        header_style="bold magenta",
        box=None,
        pad_edge=False,
    )
    bug_table.add_column("Bug Type", ratio=4)
    bug_table.add_column("Count", justify="right", ratio=1)
    bug_types = summary.get("bug_types")
    if isinstance(bug_types, list) and bug_types:
        for item in bug_types:
            if not isinstance(item, Mapping):
                continue
            bug_table.add_row(str(item.get("label", "unknown")), str(item.get("count", 0)))
    else:
        bug_table.add_row("No bug signatures recorded", "0")

    footer = Table.grid(padding=(0, 1))
    footer.add_row(
        Text("Results folder", style="dim"),
        Text(results_folder, style="white"),
    )

    return Panel(
        Group(stats, Text(""), bug_table, Text(""), footer),
        title="Run Complete",
        border_style="green",
    )


@dataclass
class RunDashboard:
    target: str
    configured_workers: int
    results_folder: str
    max_iterations: int | None
    max_hours: float | None
    started_at: float = field(default_factory=time.monotonic)
    total_results: int = 0
    interesting_results: int = 0
    crashes_found: int = 0
    timeouts_found: int = 0
    errors_found: int = 0
    unique_bugs_found: int = 0
    covered_branches_total: int = 0
    total_branches: int = 0
    unique_covered_arcs: int = 0
    covered_lines_total: int = 0
    total_lines: int = 0
    total_edges: int = 0
    coverage_backend: str = ""
    coverage_source_kind: str = ""
    qemu_bitmap_slots_total: int = 0
    scheduler_size: int = 0
    queue_size: int = 0
    pending_jobs: int = 0
    active_workers: int = 0
    busy_workers: int = 0
    last_event: str = "warming up"
    status: str = "RUNNING"
    llm_state: str = "idle"
    llm_source: str = ""
    llm_generated_count: int = 0
    llm_seed_previews: list[str] = field(default_factory=list)
    last_mutated_input: str = ""
    memory_rss_total: str = ""
    memory_rss_details: str = ""
    newest_coverage_branch: str = ""
    crash_output: str = ""
    interesting_score_threshold: float = 0.5

    def __post_init__(self) -> None:
        self.active_workers = self.configured_workers

    def snapshot(self) -> dict[str, object]:
        return {
            "target": self.target,
            "results_folder": self.results_folder,
            "status": self.status,
            "total_results": self.total_results,
            "interesting_results": self.interesting_results,
            "crashes_found": self.crashes_found,
            "timeouts_found": self.timeouts_found,
            "errors_found": self.errors_found,
            "unique_bugs_found": self.unique_bugs_found,
            "covered_branches_total": self.covered_branches_total,
            "total_branches": self.total_branches,
            "unique_covered_arcs": self.unique_covered_arcs,
            "covered_lines_total": self.covered_lines_total,
            "total_lines": self.total_lines,
            "total_edges": self.total_edges,
            "coverage_backend": self.coverage_backend,
            "coverage_source_kind": self.coverage_source_kind,
            "qemu_bitmap_slots_total": self.qemu_bitmap_slots_total,
            "scheduler_size": self.scheduler_size,
            "queue_size": self.queue_size,
            "pending_jobs": self.pending_jobs,
            "active_workers": self.active_workers,
            "busy_workers": self.busy_workers,
            "last_event": self.last_event,
            "last_mutated_input": self.last_mutated_input,
            "newest_coverage_branch": self.newest_coverage_branch,
            "memory_rss_total": self.memory_rss_total,
            "memory_rss_details": self.memory_rss_details,
            "crash_output": self.crash_output,
            "llm_state": self.llm_state,
            "llm_source": self.llm_source,
            "llm_generated_count": self.llm_generated_count,
            "llm_seed_previews": list(self.llm_seed_previews),
            "max_iterations": self.max_iterations,
            "max_hours": self.max_hours,
            "elapsed_seconds": self._elapsed_seconds(),
            "exec_per_second": self._exec_rate(),
        }

    def refresh_counts_from_db(self, db_path: Path | str, *, target: str | None = None) -> None:
        path = Path(db_path)
        if not path.is_file():
            return
        from core.db_utils import get_run_summary
        from core.sqlite_conn import open_results_db

        conn = open_results_db(path)
        try:
            summary = get_run_summary(conn, target=target or self.target)
        finally:
            conn.close()
        self._apply_run_summary(summary)

    def _apply_run_summary(self, summary: Mapping[str, Any]) -> None:
        self.total_results = int(summary.get("total_results", self.total_results) or 0)
        self.interesting_results = int(
            summary.get("interesting_results", self.interesting_results) or 0
        )
        status_counts = summary.get("status_counts")
        if not isinstance(status_counts, Mapping):
            status_counts = {}
        self.crashes_found = int(status_counts.get("crash", self.crashes_found) or 0)
        self.timeouts_found = int(status_counts.get("timeout", self.timeouts_found) or 0)
        self.errors_found = int(status_counts.get("error", self.errors_found) or 0)
        self.unique_bugs_found = int(
            summary.get("unique_bug_count", self.unique_bugs_found) or 0
        )

    def save_artifacts(self, dest_dir: Path | str) -> tuple[Path, Path]:
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        snapshot_path = dest / "final_dashboard_stats.json"
        text_path = dest / "final_dashboard.txt"
        db_path = dest / "runs.db"
        self.refresh_counts_from_db(db_path, target=self.target)

        with snapshot_path.open("w", encoding="utf-8") as handle:
            json.dump(self.snapshot(), handle, indent=2, sort_keys=True)
            handle.write("\n")

        export_console = Console(record=True, width=180)
        export_console.print(self.render())
        text_path.write_text(export_console.export_text(), encoding="utf-8")
        return snapshot_path, text_path

    def _elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.started_at)

    def _fmt_elapsed(self) -> str:
        return self._fmt_duration(self._elapsed_seconds())

    def _fmt_duration(self, seconds: float) -> str:
        if seconds < 60:
            return f"{seconds:.1f}s"

        minutes = seconds / 60
        if minutes < 60:
            return f"{minutes:.1f}m"

        hours = minutes / 60
        return f"{hours:.2f}h"

    def _exec_rate(self) -> float:
        elapsed = self._elapsed_seconds()
        if elapsed <= 0:
            return 0.0
        return self.total_results / elapsed

    def update_status(self, status: str, *, event: str | None = None) -> None:
        self.status = status
        if event:
            self.last_event = event

    def update_worker_count(self, *, active_workers: int) -> None:
        self.active_workers = max(0, int(active_workers))

    def update_busy_workers(self, *, busy_workers: int) -> None:
        self.busy_workers = max(0, int(busy_workers))

    def start_llm_generation(self, *, source: str, requested: int) -> None:
        self.llm_state = "generating"
        self.llm_source = source
        self.llm_generated_count = requested
        self.status = "GENERATING"
        self.last_event = f"{source} requesting {requested} seeds"

    def finish_llm_generation(self, *, source: str, seeds: list[str]) -> None:
        self.llm_state = "ready"
        self.llm_source = source
        self.llm_generated_count = len(seeds)
        self.llm_seed_previews = [self._preview_seed(seed) for seed in seeds[:3]]
        self.status = "RUNNING"
        self.last_event = f"{source} added {len(seeds)} seeds"

    def fail_llm_generation(self, *, source: str, event: str) -> None:
        self.llm_state = "failed"
        self.llm_source = source
        self.status = "RUNNING"
        self.last_event = event

    def _preview_seed(self, seed: str, *, limit: int = 56) -> str:
        cleaned = " ".join(seed.split())
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: limit - 3] + "..."

    def _preview_input(self, value: str, *, limit: int = 240) -> str:
        cleaned = " ".join(value.split())
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: limit - 3] + "..."

    def record_schedule(
        self,
        *,
        pending_jobs: int,
        scheduler_size: int,
        queue_size: int,
        event: str,
    ) -> None:
        self.pending_jobs = pending_jobs
        self.scheduler_size = scheduler_size
        self.queue_size = queue_size
        self.last_event = event

    def update_memory_telemetry(self, *, total_rss: str, details: str) -> None:
        self.memory_rss_total = total_rss
        self.memory_rss_details = details

    def record_result(
        self,
        *,
        iteration: int,
        status: str,
        score: float,
        new_coverage: bool,
        new_bug: bool,
        covered_branches: int,
        total_branches: int = 0,
        coverage_backend: str = "",
        coverage_source_kind: str = "",
        covered_lines: int = 0,
        total_lines: int = 0,
        total_edges: int = 0,
        unique_covered_arcs: int,
        pending_jobs: int,
        scheduler_size: int,
        queue_size: int,
        event: str,
        mutated_input: str,
        newest_coverage_branch: str = "",
        bug_signature: Mapping[str, object] | None = None,
    ) -> None:
        self.total_results += 1
        self.pending_jobs = pending_jobs
        self.scheduler_size = scheduler_size
        self.queue_size = queue_size
        self.last_event = event
        self.last_mutated_input = self._preview_input(mutated_input)
        self.covered_branches_total = max(0, int(covered_branches))
        self.total_branches = max(self.total_branches, max(0, int(total_branches)))
        self.unique_covered_arcs = max(0, int(unique_covered_arcs))
        self.covered_lines_total = max(
            self.covered_lines_total, max(0, int(covered_lines))
        )
        self.total_lines = max(self.total_lines, max(0, int(total_lines)))
        self.total_edges = max(self.total_edges, max(0, int(total_edges)))
        if coverage_backend:
            self.coverage_backend = coverage_backend
        if coverage_source_kind:
            self.coverage_source_kind = coverage_source_kind
        if self.coverage_backend == "afl-qemu-showmap":
            self.qemu_bitmap_slots_total = max(0, int(unique_covered_arcs))

        if score >= self.interesting_score_threshold:
            self.interesting_results += 1
        if status == "crash":
            self.crashes_found += 1
        if status == "timeout":
            self.timeouts_found += 1
        if status == "error":
            self.errors_found += 1
        if new_bug:
            self.unique_bugs_found += 1
        if new_coverage:
            if newest_coverage_branch:
                self.newest_coverage_branch = newest_coverage_branch
        if status == "crash":
            self.crash_output = self._format_crash_output(
                iteration=iteration,
                bug_signature=bug_signature,
            )

    def _format_crash_output(
        self,
        *,
        iteration: int,
        bug_signature: Mapping[str, object] | None = None,
    ) -> str:
        if not isinstance(bug_signature, Mapping):
            bug_signature = {}
        parts = [f"iteration={iteration}"]
        exception = str(bug_signature.get("exception") or "").strip()
        message = str(bug_signature.get("message") or "").strip()
        file_name = str(bug_signature.get("file") or "").strip()
        line = str(bug_signature.get("line") or "").strip()
        bug_type = str(bug_signature.get("type") or "").strip()
        if exception:
            parts.append(f"exception={exception}")
        if bug_type:
            parts.append(f"type={bug_type}")
        if file_name:
            location = f"{file_name}:{line}" if line else file_name
            parts.append(f"location={location}")
        if message:
            parts.append(f"message={message}")
        return " | ".join(parts)

    def _preview_branch(self, value: str, *, limit: int = 72) -> str:
        cleaned = " ".join(value.split())
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: limit - 3] + "..."

    def _status_text(self) -> Text:
        styles = {
            "RUNNING": "bold cyan",
            "GENERATING": "bold magenta",
            "DONE": "bold green",
            "STOPPING": "bold yellow",
            "FAILED": "bold red",
        }
        return Text(self.status, style=styles.get(self.status, "bold white"))

    def _summary_table(self) -> Table:
        table = Table.grid(expand=True)
        covered_branches_text = str(self.covered_branches_total)
        if self.total_branches > 0:
            ratio = (self.covered_branches_total / float(self.total_branches)) * 100.0
            covered_branches_text = (
                f"{self.covered_branches_total} / {self.total_branches} ({ratio:.1f}%)"
            )
        elif self.coverage_backend == "afl-qemu-showmap":
            covered_branches_text = "n/a (QEMU)"
        edge_coverage_text = str(self.unique_covered_arcs)
        if self.total_edges > 0:
            ratio = (self.unique_covered_arcs / float(self.total_edges)) * 100.0
            edge_coverage_text = (
                f"{self.unique_covered_arcs} / {self.total_edges} ({ratio:.1f}%)"
            )
        elif self.coverage_backend == "afl-qemu-showmap":
            edge_coverage_text = "n/a (QEMU)"
        statement_coverage_text = str(self.covered_lines_total)
        if self.total_lines > 0:
            ratio = (self.covered_lines_total / float(self.total_lines)) * 100.0
            statement_coverage_text = (
                f"{self.covered_lines_total} / {self.total_lines} ({ratio:.1f}%)"
            )
        values = [
            ("Status", self._status_text()),
            ("Exec/s", Text(f"{self._exec_rate():.1f}", style="bold white")),
            ("Results", Text(str(self.total_results), style="bold white")),
            (
                "Crashes",
                Text(
                    str(self.crashes_found),
                    style="bold red" if self.crashes_found else "green",
                ),
            ),
            (
                "Unique Bugs",
                Text(
                    str(self.unique_bugs_found),
                    style="bold magenta" if self.unique_bugs_found else "green",
                ),
            ),
            (
                "Branch Coverage",
                Text(
                    covered_branches_text,
                    style="bold magenta" if self.covered_branches_total else "dim",
                ),
            ),
            (
                "Edge Coverage",
                Text(
                    edge_coverage_text,
                    style="bold magenta" if self.unique_covered_arcs else "dim",
                ),
            ),
            (
                "Statement Coverage",
                Text(
                    statement_coverage_text,
                    style="bold magenta" if self.covered_lines_total else "dim",
                ),
            ),
        ]
        if self.coverage_backend == "afl-qemu-showmap":
            values.append(
                (
                    "QEMU Bitmap Slots",
                    Text(
                        str(self.qemu_bitmap_slots_total),
                        style=(
                            "bold magenta"
                            if self.qemu_bitmap_slots_total
                            else "dim"
                        ),
                    ),
                )
            )
        for _ in values:
            table.add_column(justify="center")
        table.add_row(
            *[
                Text.assemble((label + "\n", "dim"), value)
                if isinstance(value, Text)
                else Text.assemble((label + "\n", "dim"), str(value))
                for label, value in values
            ]
        )
        return table

    def _details_table(self) -> Table:
        table = Table(
            expand=True,
            header_style="bold cyan",
            box=None,
            pad_edge=False,
        )
        table.add_column("Target", style="bold white", ratio=2)
        table.add_column("Alive Workers", justify="right")
        table.add_column("Busy Workers", justify="right")
        table.add_column("Interesting", justify="right")
        table.add_column("Timeouts", justify="right")
        table.add_column("Errors", justify="right")
        table.add_column("Pending", justify="right")
        table.add_column("Elapsed", justify="right")
        table.add_column("Budget", justify="right")
        table.add_column("RSS Total", justify="right")
        table.add_column("Last Event", ratio=4)

        timeouts_style = "bold yellow" if self.timeouts_found else "green"
        errors_style = "bold red" if self.errors_found else "green"
        pending_style = "bold cyan" if self.pending_jobs else "dim"
        if self.max_hours is not None:
            total_budget_seconds = self.max_hours * 3600
            budget_text = (
                f"{self._fmt_duration(self._elapsed_seconds())} / "
                f"{self._fmt_duration(total_budget_seconds)}"
            )
        elif self.max_iterations is not None:
            budget_text = f"{self.total_results}/{self.max_iterations} iter"
        else:
            budget_text = "open"
        table.add_row(
            self.target,
            str(self.active_workers),
            str(self.busy_workers),
            str(self.interesting_results),
            Text(str(self.timeouts_found), style=timeouts_style),
            Text(str(self.errors_found), style=errors_style),
            Text(str(self.pending_jobs), style=pending_style),
            self._fmt_elapsed(),
            budget_text,
            self.memory_rss_total or "-",
            self.last_event,
        )
        table.add_row(
            Text("Newest Coverage Branch", style="dim"),
            self._preview_branch(self.newest_coverage_branch) or "-",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
        )
        if self.coverage_source_kind:
            table.add_row(
                Text("Coverage Source", style="dim"),
                self.coverage_source_kind,
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
            )
        if self.coverage_backend:
            table.add_row(
                Text("Coverage Backend", style="dim"),
                self.coverage_backend,
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
            )
        return table

    def _memory_panel(self) -> Panel | None:
        if not self.memory_rss_details:
            return None
        text = Text(self.memory_rss_details, style="white")
        return Panel(text, title="Memory RSS", border_style="green")

    def _llm_panel(self) -> Panel | None:
        if self.llm_state == "idle" and not self.llm_seed_previews:
            return None

        table = Table.grid(expand=True, padding=(0, 1))
        table.add_column(style="bold cyan", justify="right")
        table.add_column(style="white")
        state_style = {
            "generating": "bold magenta",
            "ready": "bold green",
            "failed": "bold yellow",
        }.get(self.llm_state, "white")
        table.add_row("state", Text(self.llm_state.upper(), style=state_style))
        if self.llm_source:
            table.add_row("source", self.llm_source)
        table.add_row("count", str(self.llm_generated_count))
        if self.llm_seed_previews:
            previews = "\n".join(
                f"{idx + 1}. {seed}" for idx, seed in enumerate(self.llm_seed_previews)
            )
            table.add_row("previews", previews)
        return Panel(table, title="Generated Seeds", border_style="magenta")

    def _last_mutation_panel(self) -> Panel | None:
        if not self.last_mutated_input:
            return None
        text = Text(self.last_mutated_input, style="white")
        return Panel(text, title="Last Mutated Input", border_style="yellow")

    def _crash_output_panel(self) -> Panel | None:
        if not self.crash_output:
            return None
        table = Table(
            expand=True,
            header_style="bold red",
            box=None,
            pad_edge=False,
        )
        table.add_column("Crash Output", style="bold white")
        table.add_row(self._preview_input(self.crash_output, limit=320))
        return Panel(table, title="Crash Output", border_style="red")

    def render(self) -> RenderableType:
        footer = Table.grid(padding=(0, 1))
        footer.add_row(
            Text("Results folder", style="dim"),
            Text(self.results_folder, style="white"),
            Text("Scheduler Size", style="dim"),
            Text(str(self.scheduler_size), style="white"),
            Text("Queue Size", style="dim"),
            Text(str(self.queue_size), style="white"),
        )
        panels: list[RenderableType] = [
            Panel(self._summary_table(), title="Run Summary", border_style="blue"),
            Panel(self._details_table(), title="Live Table", border_style="cyan"),
        ]
        last_mutation_panel = self._last_mutation_panel()
        if last_mutation_panel is not None:
            panels.append(last_mutation_panel)
        llm_panel = self._llm_panel()
        if llm_panel is not None:
            panels.append(llm_panel)
        memory_panel = self._memory_panel()
        if memory_panel is not None:
            panels.append(memory_panel)
        panels.append(Panel(footer, border_style="dim"))
        crash_output_panel = self._crash_output_panel()
        if crash_output_panel is not None:
            panels.append(crash_output_panel)
        group = Group(*panels)
        return group
