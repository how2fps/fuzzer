"""
Regex / grammar no-seed corpus version.

Uses the standard manifest-backed corpus loader, but the runner interprets this
version name to skip startup preload from the corpus and instead generate
initial scheduler seeds from the grammar_ast mutator's seedless generator.
"""
from __future__ import annotations

from ..corpus import SeedCorpus

__all__ = ["SeedCorpus"]
