from __future__ import annotations

from datetime import datetime, timezone
from multiprocessing import current_process

from core.config import get_run_plan, print_config
from core.fuzzer_runner import run_fuzzer
from core.paths import RESULTS_DIR
from core.batch_report import generate_batch_report


def main() -> None:
    entries, runs_per_config = get_run_plan()
    batch_timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    batch_folder = RESULTS_DIR / f"batch_{batch_timestamp}"
    batch_folder.mkdir(parents=True, exist_ok=True)

    for config_path, config in entries:
        config_label = config_path.stem if config_path is not None else "cli"
        config_folder = batch_folder / config_label
        config_folder.mkdir(parents=True, exist_ok=True)
        for run_index in range(runs_per_config):
            run_timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            run_folder = config_folder / f"run_{run_index + 1}_{run_timestamp}"
            if runs_per_config > 1:
                print(f"\n--- Run {run_index + 1}/{runs_per_config} for config: {config_path or 'CLI'} ---")
            elif config_path:
                print(f"\n--- Config: {config_path} ---")
            print_config(config)
            run_fuzzer(config, results_folder=run_folder, config_path=config_path)

    generate_batch_report(batch_folder=batch_folder)


if __name__ == "__main__":
    if current_process().name == "MainProcess":
        main()
