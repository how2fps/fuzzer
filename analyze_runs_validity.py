from __future__ import annotations

import argparse
import ipaddress
import json
from pathlib import Path

import matplotlib
import pandas as pd


matplotlib.use("Agg")
import matplotlib.pyplot as plt

SUPPORTED_FORMATS = ("ipv4", "ipv6", "json")
DEFAULT_TARGET_BINS = 40


def classify_input_validity(mutated_input: object, fmt: str) -> bool:
    text = "" if mutated_input is None else str(mutated_input)

    try:
        if fmt == "json":
            json.loads(text)
            return True
        if fmt == "ipv4":
            ipaddress.IPv4Address(text.strip())
            return True
        if fmt == "ipv6":
            ipaddress.IPv6Address(text.strip())
            return True
    except (ValueError, TypeError, json.JSONDecodeError):
        return False

    raise ValueError(
        f"Unsupported format {fmt!r}. Expected one of: {', '.join(SUPPORTED_FORMATS)}"
    )


def load_runs_dataframe(runs_csv: Path) -> pd.DataFrame:
    df = pd.read_csv(runs_csv, keep_default_na=False)
    required_columns = {"mutated_input", "created_at"}
    missing = sorted(required_columns - set(df.columns))
    if missing:
        raise ValueError(
            f"Missing required column(s) in {runs_csv}: {', '.join(missing)}"
        )
    if df.empty:
        raise ValueError(f"No rows found in {runs_csv}")

    df = df.copy()
    df["created_at"] = pd.to_datetime(df["created_at"], utc=True, errors="coerce")
    df = df[df["created_at"].notna()].copy()
    if df.empty:
        raise ValueError(f"No parseable created_at timestamps found in {runs_csv}")

    df.sort_values(["created_at", "iteration"], inplace=True, kind="stable")
    return df


def _coerce_metric_column(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series([0] * len(df), index=df.index, dtype="int64")
    values = pd.to_numeric(df[column], errors="coerce").fillna(0)
    return values.astype("int64")


def choose_bin_seconds(elapsed_seconds: pd.Series, requested_bin_seconds: float | None) -> float:
    if requested_bin_seconds is not None:
        if requested_bin_seconds <= 0:
            raise ValueError("--bin-seconds must be > 0")
        return requested_bin_seconds

    span_seconds = float(elapsed_seconds.max()) if not elapsed_seconds.empty else 0.0
    if span_seconds <= 0:
        return 1.0
    return max(span_seconds / DEFAULT_TARGET_BINS, 1.0)


def build_binned_validity_table(
    df: pd.DataFrame,
    *,
    fmt: str,
    bin_seconds: float | None = None,
) -> tuple[pd.DataFrame, float]:
    working = df.copy()
    start_time = working["created_at"].min()
    working["elapsed_seconds"] = (
        working["created_at"] - start_time
    ).dt.total_seconds()
    working["is_valid"] = working["mutated_input"].map(
        lambda value: classify_input_validity(value, fmt)
    )

    resolved_bin_seconds = choose_bin_seconds(
        working["elapsed_seconds"], requested_bin_seconds=bin_seconds
    )
    working["bin_start_seconds"] = (
        (working["elapsed_seconds"] // resolved_bin_seconds) * resolved_bin_seconds
    ).astype(float)

    summary = (
        working.groupby("bin_start_seconds", as_index=False)
        .agg(
            total=("is_valid", "size"),
            valid_count=("is_valid", "sum"),
        )
        .sort_values("bin_start_seconds", kind="stable")
    )
    summary["invalid_count"] = summary["total"] - summary["valid_count"]
    summary["bin_mid_seconds"] = summary["bin_start_seconds"] + (
        resolved_bin_seconds / 2.0
    )
    summary["valid_pct"] = (summary["valid_count"] / summary["total"]) * 100.0
    summary["invalid_pct"] = (summary["invalid_count"] / summary["total"]) * 100.0
    summary["format"] = fmt
    return summary, resolved_bin_seconds


def build_cumulative_validity_table(df: pd.DataFrame, *, fmt: str) -> pd.DataFrame:
    working = df.copy()
    start_time = working["created_at"].min()
    working["elapsed_seconds"] = (
        working["created_at"] - start_time
    ).dt.total_seconds()
    working["is_valid"] = working["mutated_input"].map(
        lambda value: classify_input_validity(value, fmt)
    )
    working["valid_count"] = working["is_valid"].astype(int).cumsum()
    working["total"] = range(1, len(working) + 1)
    working["invalid_count"] = working["total"] - working["valid_count"]
    working["valid_pct"] = (working["valid_count"] / working["total"]) * 100.0
    working["invalid_pct"] = (working["invalid_count"] / working["total"]) * 100.0
    working["format"] = fmt
    return working[
        [
            "elapsed_seconds",
            "total",
            "valid_count",
            "invalid_count",
            "valid_pct",
            "invalid_pct",
            "format",
        ]
    ].reset_index(drop=True)


def build_cumulative_coverage_table(df: pd.DataFrame) -> pd.DataFrame:
    working = df.copy()
    start_time = working["created_at"].min()
    working["elapsed_seconds"] = (
        working["created_at"] - start_time
    ).dt.total_seconds()
    working["unique_covered_arcs"] = _coerce_metric_column(
        working, "unique_covered_arcs"
    )
    working["covered_branches"] = _coerce_metric_column(working, "covered_branches")
    return working[
        ["elapsed_seconds", "iteration", "unique_covered_arcs", "covered_branches"]
    ].reset_index(drop=True)


def plot_validity_chart(
    summary: pd.DataFrame,
    *,
    fmt: str,
    runs_csv: Path,
    output_path: Path,
    mode: str,
    bin_seconds: float | None = None,
) -> None:
    fig, ax = plt.subplots(figsize=(12, 6))

    x_column = "bin_mid_seconds" if mode == "binned" else "elapsed_seconds"
    x = summary[x_column]

    ax.fill_between(
        x,
        0,
        summary["valid_pct"],
        color="#2a9d8f",
        alpha=0.75,
        label="Valid %",
    )
    ax.fill_between(
        x,
        summary["valid_pct"],
        100,
        color="#e76f51",
        alpha=0.75,
        label="Invalid %",
    )
    ax.plot(x, summary["valid_pct"], color="#1f6f66", linewidth=2)
    ax.plot(x, summary["invalid_pct"], color="#b44b34", linewidth=2)

    title = f"{fmt.upper()} mutated input validity over time"
    if mode == "binned" and bin_seconds is not None:
        title += f" ({bin_seconds:.2f}s bins)"
    elif mode == "cumulative":
        title += " (cumulative)"

    ax.set_title(title)
    ax.set_xlabel("Elapsed time (seconds)")
    ax.set_ylabel("Share of mutated inputs (%)")
    ax.set_ylim(0, 100)
    ax.set_xlim(left=0)
    ax.grid(alpha=0.25, linestyle=":")
    ax.legend(loc="upper right")

    total_inputs = int(summary["total"].sum()) if mode == "binned" else int(summary["total"].iloc[-1])
    valid_inputs = int(summary["valid_count"].sum()) if mode == "binned" else int(summary["valid_count"].iloc[-1])
    invalid_inputs = total_inputs - valid_inputs
    subtitle = (
        f"Source: {runs_csv.name} | total={total_inputs} | "
        f"valid={valid_inputs} | invalid={invalid_inputs}"
    )
    fig.text(0.5, 0.01, subtitle, ha="center", fontsize=10)

    fig.tight_layout(rect=(0, 0.04, 1, 1))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=160)
    plt.close(fig)


def plot_coverage_chart(
    summary: pd.DataFrame,
    *,
    runs_csv: Path,
    unique_arcs_output_path: Path,
    covered_branches_output_path: Path,
) -> None:
    x = summary["elapsed_seconds"]
    final_unique_arcs = int(summary["unique_covered_arcs"].iloc[-1]) if not summary.empty else 0
    final_covered_branches = int(summary["covered_branches"].iloc[-1]) if not summary.empty else 0

    def _plot_single_metric(
        *,
        y: pd.Series,
        title: str,
        y_label: str,
        color: str,
        final_value: int,
        output_path: Path,
    ) -> None:
        fig, ax = plt.subplots(figsize=(12, 6))

        ax.plot(
            x,
            y,
            color=color,
            linewidth=2.5,
        )

        ax.set_title(title)
        ax.set_xlabel("Elapsed time (seconds)")
        ax.set_ylabel(y_label)
        ax.set_xlim(left=0)
        ax.set_ylim(bottom=0)
        ax.grid(alpha=0.25, linestyle=":")

        subtitle = f"Source: {runs_csv.name} | final={final_value}"
        fig.text(0.5, 0.01, subtitle, ha="center", fontsize=10)

        fig.tight_layout(rect=(0, 0.04, 1, 1))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(output_path, dpi=160)
        plt.close(fig)

    _plot_single_metric(
        y=summary["unique_covered_arcs"],
        title="Unique arcs coverage growth over time",
        y_label="Unique covered arcs",
        color="#264653",
        final_value=final_unique_arcs,
        output_path=unique_arcs_output_path,
    )
    _plot_single_metric(
        y=summary["covered_branches"],
        title="Covered branches growth over time",
        y_label="Covered branches",
        color="#f4a261",
        final_value=final_covered_branches,
        output_path=covered_branches_output_path,
    )


def default_output_path(runs_csv: Path, fmt: str, mode: str) -> Path:
    stem = f"{runs_csv.stem}_{fmt}_validity_{mode}"
    return runs_csv.with_name(f"{stem}.png")


def default_coverage_output_path(runs_csv: Path) -> Path:
    return runs_csv.with_name(f"{runs_csv.stem}_coverage_growth.png")


def split_coverage_output_paths(coverage_output_path: Path) -> tuple[Path, Path]:
    suffix = coverage_output_path.suffix if coverage_output_path.suffix else ".png"
    stem = coverage_output_path.stem if coverage_output_path.stem else "coverage_growth"
    parent = coverage_output_path.parent
    unique_arcs_path = parent / f"{stem}_unique_arcs{suffix}"
    covered_branches_path = parent / f"{stem}_covered_branches{suffix}"
    return unique_arcs_path, covered_branches_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Analyse a runs.csv and plot the percentage of valid vs invalid "
            "mutated inputs over time for json, ipv4, or ipv6."
        )
    )
    parser.add_argument("runs_csv", type=Path, help="Path to a runs.csv file")
    parser.add_argument(
        "--format",
        required=True,
        choices=SUPPORTED_FORMATS,
        help="How to validate mutated_input values",
    )
    parser.add_argument(
        "--mode",
        choices=("binned", "cumulative"),
        default="binned",
        help="Plot per-time-bin percentages or cumulative percentages",
    )
    parser.add_argument(
        "--bin-seconds",
        type=float,
        default=None,
        help="Bin width in seconds for binned mode. Defaults to an automatic value.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output PNG path. Defaults next to runs.csv.",
    )
    parser.add_argument(
        "--summary-csv",
        type=Path,
        default=None,
        help="Optional path for the aggregated summary CSV.",
    )
    parser.add_argument(
        "--coverage-output",
        type=Path,
        default=None,
        help="Optional path for the coverage growth PNG. Defaults next to runs.csv.",
    )
    parser.add_argument(
        "--coverage-summary-csv",
        type=Path,
        default=None,
        help="Optional path for the coverage growth summary CSV.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    runs_csv = args.runs_csv.resolve()
    df = load_runs_dataframe(runs_csv)

    if args.mode == "binned":
        summary, resolved_bin_seconds = build_binned_validity_table(
            df,
            fmt=args.format,
            bin_seconds=args.bin_seconds,
        )
    else:
        if args.bin_seconds is not None:
            raise ValueError("--bin-seconds can only be used with --mode binned")
        summary = build_cumulative_validity_table(df, fmt=args.format)
        resolved_bin_seconds = None

    output_path = (
        args.output.resolve()
        if args.output is not None
        else default_output_path(runs_csv, args.format, args.mode)
    )
    summary_csv = (
        args.summary_csv.resolve()
        if args.summary_csv is not None
        else output_path.with_suffix(".csv")
    )
    coverage_output_path = (
        args.coverage_output.resolve()
        if args.coverage_output is not None
        else default_coverage_output_path(runs_csv)
    )
    unique_arcs_output_path, covered_branches_output_path = split_coverage_output_paths(
        coverage_output_path
    )
    coverage_summary_csv = (
        args.coverage_summary_csv.resolve()
        if args.coverage_summary_csv is not None
        else coverage_output_path.with_suffix(".csv")
    )
    coverage_summary = build_cumulative_coverage_table(df)

    plot_validity_chart(
        summary,
        fmt=args.format,
        runs_csv=runs_csv,
        output_path=output_path,
        mode=args.mode,
        bin_seconds=resolved_bin_seconds,
    )
    plot_coverage_chart(
        coverage_summary,
        runs_csv=runs_csv,
        unique_arcs_output_path=unique_arcs_output_path,
        covered_branches_output_path=covered_branches_output_path,
    )
    summary.to_csv(summary_csv, index=False)
    coverage_summary.to_csv(coverage_summary_csv, index=False)

    print(f"Wrote chart to {output_path}")
    print(f"Wrote summary to {summary_csv}")
    print(f"Wrote unique arcs chart to {unique_arcs_output_path}")
    print(f"Wrote covered branches chart to {covered_branches_output_path}")
    print(f"Wrote coverage summary to {coverage_summary_csv}")


if __name__ == "__main__":
    main()
