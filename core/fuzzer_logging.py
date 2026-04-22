"""Logging for the fuzzer: coordinator uses tqdm-safe output, workers log to files."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from tqdm import tqdm

FUZZER_LOGGER_NAME = "fuzzer"
WORKER_LOG_FILENAME = "worker.log"

_COORDINATOR_LOG_FORMATTER = logging.Formatter("%(message)s")
_WORKER_LOG_FORMATTER = logging.Formatter(
    "%(asctime)s %(process)d %(levelname)s %(message)s"
)


class TqdmWriteHandler(logging.Handler):
    """
    Emit log records via tqdm.write(...) so output does not break tqdm bars.

    Safe when no bar is active (tqdm.write behaves like print to stderr).
    """

    def __init__(self, level: int = logging.NOTSET) -> None:
        super().__init__(level)
        self.stream = sys.stderr
        # logging.Handler does not define terminator (StreamHandler does); we need it for emit().
        self.terminator = "\n"

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            tqdm.write(msg, file=self.stream, end=self.terminator)
        except Exception:
            self.handleError(record)


def get_fuzzer_logger() -> logging.Logger:
    return logging.getLogger(FUZZER_LOGGER_NAME)


def _replace_handlers(
    logger: logging.Logger,
    *,
    handlers: list[logging.Handler],
    level: int,
) -> logging.Logger:
    logger.setLevel(level)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    for handler in handlers:
        handler.setLevel(level)
        logger.addHandler(handler)
    return logger


def worker_log_path(*, results_folder: str | Path, worker_id: int) -> Path:
    return (
        Path(results_folder)
        / ".worker_cwd"
        / f"w{worker_id}"
        / "logs"
        / WORKER_LOG_FILENAME
    )


def configure_fuzzer_logging(*, level: int = logging.INFO) -> logging.Logger:
    """
    Configure the coordinator logger to emit tqdm-safe terminal output.
    """
    handler = TqdmWriteHandler(level=level)
    handler.setFormatter(_COORDINATOR_LOG_FORMATTER)
    return _replace_handlers(get_fuzzer_logger(), handlers=[handler], level=level)


def configure_worker_logging(
    *,
    results_folder: str | Path,
    worker_id: int,
    level: int = logging.INFO,
) -> logging.Logger:
    """
    Reconfigure a forked worker to log to its own file instead of sharing tqdm's lock.
    """
    log_path = worker_log_path(results_folder=results_folder, worker_id=worker_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(_WORKER_LOG_FORMATTER)
    return _replace_handlers(get_fuzzer_logger(), handlers=[handler], level=level)
