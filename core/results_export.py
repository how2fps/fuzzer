from __future__ import annotations

import csv
import os
import shutil
import subprocess
import sys
from pathlib import Path

from core.db_utils import get_inputs_for_unique_error_line_pairs
from core.fuzzer_logging import get_fuzzer_logger
from core.sqlite_conn import open_results_db
from core.paths import JSON_DECODER_STV_SCRIPT, JSON_DECODER_TARGET_DIR
from core.target_artifacts import copy_bug_counts_csv_if_present


def export_results(
    *,
    results_folder: Path,
    db_path: Path,
    target: str,
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
                        "exception", "line", "file", "bug_type", "seed_id", "seed_text",
                        "mutated_input", "status", "iteration", "isinteresting_score",
                    ],
                )
                w.writeheader()
                w.writerows(pairs)
        else:
            with open(pairs_path, "w", newline="", encoding="utf-8") as f:
                f.write(
                    "exception,line,file,bug_type,seed_id,seed_text,mutated_input,status,iteration,isinteresting_score\n")

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

    if target == "json-decoder" and JSON_DECODER_STV_SCRIPT.is_file():
        get_fuzzer_logger().info(
            "Running json_decoder_stv.py for each input that triggered a unique (error, line)"
        )
        stv_logs_dir = JSON_DECODER_TARGET_DIR / "logs"
        stv_logs_dir.mkdir(parents=True, exist_ok=True)
        stv_csv = stv_logs_dir / "bug_counts.csv"
        if stv_csv.is_file():
            stv_csv.unlink()
        coverage_file = str(
            (results_folder / ".coverage_buggy_json").resolve())
        for rec in pairs:
            get_fuzzer_logger().info(
                "Running json_decoder_stv.py with input: %s",
                rec.get("mutated_input"),
            )
            input_text = rec.get("mutated_input") or ""
            if not input_text:
                continue
            env = os.environ.copy()
            env["PYTHONPATH"] = os.pathsep.join(
                [str(JSON_DECODER_TARGET_DIR), env.get("PYTHONPATH", "")]
            )
            proc = subprocess.run(
                [
                    sys.executable,
                    str(JSON_DECODER_STV_SCRIPT),
                    f"--str-json={input_text}",
                    "--show-coverage",
                    "--coverage-file",
                    coverage_file,
                ],
                cwd=str(JSON_DECODER_TARGET_DIR),
                env=env,
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0 and proc.stderr:
                get_fuzzer_logger().warning("STV script stderr: %s", proc.stderr)

    copy_bug_counts_csv_if_present(target=target, results_folder=results_folder)

