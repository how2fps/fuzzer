"""
TEMPLATE: Copy to a new file (e.g. status_only.py), implement compute_interestingness,
then in versions/__init__.py add:
    from . import status_only
    REGISTRY["status_only"] = status_only.compute_interestingness
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def compute_interestingness(*, result: Mapping[str, Any]) -> float:
    """
    Compute an interestingness score in [0.0, 1.0] for a single fuzzing run result.

    result: top-level dict from parser.run_parser(...), e.g.:
        {"closed_result": {"status": str, "bug_signature": dict?, ...}, "open_result": ...?}
    """
    # TODO: implement your scoring logic
    _ = result  # use closed_result, open_result, etc.
    return 0.0
