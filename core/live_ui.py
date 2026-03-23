from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from collections.abc import Mapping

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
        ("limit", _fmt_limit(config)),
        ("timeout", f"{config['timeout']}s"),
        ("seed", str(config["rng_seed"])),
        ("parser", config["parser_version"]),
        ("interestingness", config["isinteresting_version"]),
        ("power scheduler", config["power_scheduler_version"]),
        ("seed corpus", config["seed_corpus_version"]),
        ("LLM fallback", _fmt_bool(config["llm_seed_fallback"])),
    ]
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
    workers: int
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
    new_coverage_events: int = 0
    queue_depth: int = 0
    pending_jobs: int = 0
    last_event: str = "warming up"
    status: str = "RUNNING"
    # Only treat a result as "interesting" when its score is sufficiently high.
    interesting_score_threshold: float = 0.5

    def _elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.started_at)

    def _fmt_elapsed(self) -> str:
        seconds = self._elapsed_seconds()
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

    def record_schedule(self, *, pending_jobs: int, queue_depth: int, event: str) -> None:
        self.pending_jobs = pending_jobs
        self.queue_depth = queue_depth
        self.last_event = event

    def record_result(
        self,
        *,
        status: str,
        score: float,
        new_coverage: bool,
        new_bug: bool,
        pending_jobs: int,
        queue_depth: int,
        event: str,
    ) -> None:
        self.total_results += 1
        self.pending_jobs = pending_jobs
        self.queue_depth = queue_depth
        self.last_event = event

        if score > self.interesting_score_threshold:
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
            self.new_coverage_events += 1

    def _status_text(self) -> Text:
        styles = {
            "RUNNING": "bold cyan",
            "DONE": "bold green",
            "STOPPING": "bold yellow",
            "FAILED": "bold red",
        }
        return Text(self.status, style=styles.get(self.status, "bold white"))

    def _summary_table(self) -> Table:
        table = Table.grid(expand=True)
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
                "Coverage",
                Text(
                    str(self.new_coverage_events),
                    style="bold magenta" if self.new_coverage_events else "dim",
                ),
            ),
        ]
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
        table.add_column("Workers", justify="right")
        table.add_column("Interesting", justify="right")
        table.add_column("Timeouts", justify="right")
        table.add_column("Errors", justify="right")
        table.add_column("Pending", justify="right")
        table.add_column("Elapsed", justify="right")
        table.add_column("Last Event", ratio=4)

        timeouts_style = "bold yellow" if self.timeouts_found else "green"
        errors_style = "bold red" if self.errors_found else "green"
        pending_style = "bold cyan" if self.pending_jobs else "dim"
        limit = (
            f"{self.max_hours}h"
            if self.max_hours is not None
            else str(self.max_iterations)
            if self.max_iterations is not None
            else "open"
        )
        table.add_row(
            self.target,
            str(self.workers),
            str(self.interesting_results),
            Text(str(self.timeouts_found), style=timeouts_style),
            Text(str(self.errors_found), style=errors_style),
            Text(str(self.pending_jobs), style=pending_style),
            f"{self._fmt_elapsed()} / {limit}",
            self.last_event,
        )
        return table

    def render(self) -> RenderableType:
        footer = Table.grid(padding=(0, 1))
        footer.add_row(
            Text("Results folder", style="dim"),
            Text(self.results_folder, style="white"),
            Text("Queue depth", style="dim"),
            Text(str(self.queue_depth), style="white"),
        )
        group = Group(
            Panel(self._summary_table(), title="Run Summary", border_style="blue"),
            Panel(self._details_table(), title="Live Table", border_style="cyan"),
            Panel(footer, border_style="dim"),
        )
        return group
