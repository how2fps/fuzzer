"""
Base parser: run fuzzer input against targets, return normalized JSON results.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..parser import (
    DEFAULT_TIMEOUT,
    TARGETS,
    run_parser,
)

# Re-export for the version interface
__all__ = ["run_parser", "TARGETS", "DEFAULT_TIMEOUT", "COVERAGE_TARGET_NAME", "JSON_OPEN_SCRIPT"]

# Optional exports used by some callers
from ..parser import COVERAGE_TARGET_NAME, JSON_OPEN_SCRIPT
