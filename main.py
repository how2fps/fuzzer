from __future__ import annotations

from multiprocessing import current_process

from core.config import build_config, print_config
from core.fuzzer_runner import run_fuzzer


def main() -> None:
    config = build_config()
    print_config(config)
    run_fuzzer(config)


if __name__ == "__main__":
    if current_process().name == "MainProcess":
        main()
