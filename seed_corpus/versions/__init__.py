"""
Registry of seed corpus implementations and their startup behavior.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Type

from . import base, llm_bootstrap, regex_noseed

CorpusLoader = Type[Any]
StartupMode = Literal["corpus_preload", "llm_bootstrap", "grammar_bootstrap"]


@dataclass(frozen=True)
class SeedCorpusVersionSpec:
    name: str
    loader: CorpusLoader
    startup_mode: StartupMode = "corpus_preload"
    aliases: tuple[str, ...] = ()


_SPECS: tuple[SeedCorpusVersionSpec, ...] = (
    SeedCorpusVersionSpec(
        name="base",
        loader=base.SeedCorpus,
        startup_mode="corpus_preload",
    ),
    SeedCorpusVersionSpec(
        name="llm_bootstrap",
        loader=llm_bootstrap.SeedCorpus,
        startup_mode="llm_bootstrap",
    ),
    SeedCorpusVersionSpec(
        name="regex-noseed",
        loader=regex_noseed.SeedCorpus,
        startup_mode="grammar_bootstrap",
        aliases=("regex_noseed",),
    ),
)

REGISTRY: dict[str, SeedCorpusVersionSpec] = {spec.name: spec for spec in _SPECS}
ALIASES: dict[str, str] = {
    alias: spec.name
    for spec in _SPECS
    for alias in spec.aliases
}


def canonicalize_version(version: str) -> str:
    return ALIASES.get(version, version)


def get_version_spec(version: str) -> SeedCorpusVersionSpec:
    canonical = canonicalize_version(version)
    if canonical not in REGISTRY:
        raise ValueError(
            f"unknown seed_corpus version {version!r}; choices: {sorted(REGISTRY)}"
        )
    return REGISTRY[canonical]


def get_corpus_loader(version: str) -> CorpusLoader:
    return get_version_spec(version).loader


def list_versions() -> list[str]:
    return sorted(REGISTRY.keys())


__all__ = [
    "SeedCorpusVersionSpec",
    "canonicalize_version",
    "get_corpus_loader",
    "get_version_spec",
    "list_versions",
]
