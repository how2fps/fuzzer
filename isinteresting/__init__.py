"""
Interestingness scoring for fuzzer results. Pluggable versions for ablation.
"""
from __future__ import annotations

from .versions import get_compute_interestingness, list_versions
from .versions.base import compute_interestingness

__all__ = [
    "compute_interestingness",
    "get_compute_interestingness",
    "list_versions",
]
