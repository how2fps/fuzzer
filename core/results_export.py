from __future__ import annotations

import csv
from pathlib import Path

from core.db_utils import get_inputs_for_unique_error_line_pairs
from core.fuzzer_logging import get_fuzzer_logger
from core.sqlite_conn import open_results_db
from core.target_artifacts import copy_bug_counts_csv_if_present


def export_results(
    *,
    results_folder: Path,
    db_path: Path,
    target: str,
    copy_bug_counts: bool = True,
) -> None:
    get_fuzzer_logger().info("Exporting results to %s", results_folder)
    conn = open_results_db(db_path)
    try:
        pairs = get_inputs_for_unique_error_line_pairs(conn)
        pairs_path = results_folder / "unique_error_line_pairs.csv"
        if pairs:
            with open(pairs_path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(
                    f,
                    fieldnames=[
                        "exception",
                        "line",
                        "file",
                        "bug_type",
                        "seed_id",
                        "seed_text",
                        "mutated_input",
                        "status",
                        "iteration",
                        "isinteresting_score",
                        "datetime_executed",
                    ],
                )
                w.writeheader()
                w.writerows(pairs)
        else:
            with open(pairs_path, "w", newline="", encoding="utf-8") as f:
                f.write(
                    "exception,line,file,bug_type,seed_id,seed_text,mutated_input,"
                    "status,iteration,isinteresting_score,datetime_executed\n"
                )

        runs_path = results_folder / "runs.csv"
        cur = conn.execute(
            "SELECT iteration, seed_id, seed_text, mutated_input, status, bug_type, "
            "exception, message, file, line, isinteresting_score, target, created_at FROM runs"
        )
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        with open(runs_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(rows)
    finally:
        conn.close()

    if copy_bug_counts:
        copy_bug_counts_csv_if_present(target=target, results_folder=results_folder)

