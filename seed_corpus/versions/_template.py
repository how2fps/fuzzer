"""
TEMPLATE: Copy to a new file (e.g. filtered_corpus.py), implement or wrap SeedCorpus,
then in versions/__init__.py add:
    from . import filtered_corpus
    REGISTRY["filtered_corpus"] = filtered_corpus.SeedCorpus
"""
from __future__ import annotations

# Option A: Re-export base and customize via subclass
from ..corpus import SeedCorpus as _BaseCorpus


class SeedCorpus(_BaseCorpus):
    """
    Custom corpus loader. Must provide the same interface as seed_corpus.SeedCorpus:
    - classmethod load(...) -> SeedCorpus
    - .target(name) -> TargetSeedSet
    - .families() -> tuple[str, ...]
    """

    @classmethod
    def load(
        cls,
        corpus_dir: str | None = None,
        manifest_name: str = "manifest.json",
        **kwargs: object,
    ) -> "SeedCorpus":
        # TODO: optional custom loading (e.g. filter buckets, different path)
        # Example: return super().load(corpus_dir=corpus_dir or ..., manifest_name=manifest_name, **kwargs)
        return super().load()


# Option B: Use base as-is under a different version name (see base.py).
# Option C: Fully custom class with .load() and .target() matching the interface.
