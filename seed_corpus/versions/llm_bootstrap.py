"""
LLM bootstrap corpus version.

Uses the standard manifest-backed corpus loader, but the runner interprets this
version name to skip startup preload and bootstrap initial scheduler seeds from
the LLM based on the selected target.
"""
from __future__ import annotations

from ..corpus import SeedCorpus

__all__ = ["SeedCorpus"]
