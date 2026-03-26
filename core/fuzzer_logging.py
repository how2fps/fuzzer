"""Logging for the fuzzer: console output goes through tqdm.write so progress bars stay intact."""
from __future__ import annotations

import logging
import sys

from tqdm import tqdm

FUZZER_LOGGER_NAME = "fuzzer"


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


def configure_fuzzer_logging(*, level: int = logging.INFO) -> logging.Logger:
    """
    Attach a tqdm-safe handler to the fuzzer logger (idempotent).
    Call once at process startup (e.g. main) before any fuzzer output.
    """
    logger = get_fuzzer_logger()
    logger.setLevel(level)
    logger.propagate = False

    if any(isinstance(h, TqdmWriteHandler) for h in logger.handlers):
        return logger

    handler = TqdmWriteHandler(level=level)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return logger
