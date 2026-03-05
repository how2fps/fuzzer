from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_SEED_CORPUS_DIR = Path(__file__).resolve().parent

# Keep aliases narrow and aligned to the actual targets in this project.
DEFAULT_TARGET_ALIASES: dict[str, str] = {
    "json-decoder": "json",
    "ipv4-parser": "ipv4",
    "ipv6-parser": "ipv6",
}

# Runtime target that can consume both IPv4 and IPv6 seeds.
DEFAULT_TARGET_GROUPS: dict[str, tuple[str, ...]] = {
    "cidrize-runner": ("ipv4", "ipv6"),
}


@dataclass(frozen=True)
class Seed:
    seed_id: str
    family: str
    bucket: str
    label: str
    text: str
    tags: tuple[str, ...]
    expected: str
    ordinal: int
    fingerprint: str

    @property
    def content_bytes(self) -> bytes:
        return self.to_bytes()

    def to_bytes(self, encoding: str = "utf-8") -> bytes:
        return self.text.encode(encoding)


@dataclass(frozen=True)
class SeedBucket:
    name: str
    description: str
    seeds: tuple[Seed, ...]


class TargetSeedSet:
    def __init__(
        self,
        family: str,
        dataset_id: str,
        buckets: dict[str, SeedBucket],
        metadata: dict[str, Any],
    ) -> None:
        self.family = family
        self.dataset_id = dataset_id
        self._buckets = buckets
        self.metadata = metadata

    @property
    def buckets(self) -> tuple[str, ...]:
        return tuple(self._buckets.keys())

    @property
    def seeds(self) -> tuple[Seed, ...]:
        out: list[Seed] = []
        for bucket in self._buckets.values():
            out.extend(bucket.seeds)
        return tuple(out)

    def bucket(self, bucket_name: str) -> SeedBucket:
        try:
            return self._buckets[bucket_name]
        except KeyError as exc:
            raise KeyError(
                f"unknown bucket {bucket_name!r} for family {self.family!r}"
            ) from exc

    def sample(
        self,
        rng: random.Random | None = None,
        bucket: str | None = None,
        bucket_weights: dict[str, float] | None = None,
    ) -> Seed:
        rng = rng or random.Random()
        if bucket is not None:
            seeds = self.bucket(bucket).seeds
            if not seeds:
                raise ValueError(f"bucket {bucket!r} has no seeds")
            return rng.choice(seeds)

        bucket_names = list(self._buckets.keys())
        if not bucket_names:
            raise ValueError(f"family {self.family!r} has no buckets")

        if bucket_weights:
            weights = [max(bucket_weights.get(name, 0.0), 0.0)
                       for name in bucket_names]
            if sum(weights) > 0:
                chosen_bucket = rng.choices(
                    bucket_names, weights=weights, k=1)[0]
                return rng.choice(self._buckets[chosen_bucket].seeds)

        chosen_bucket = rng.choice(bucket_names)
        return rng.choice(self._buckets[chosen_bucket].seeds)

    def summary(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "dataset_id": self.dataset_id,
            "total_seeds": len(self.seeds),
            "bucket_counts": {
                name: len(bucket.seeds) for name, bucket in self._buckets.items()
            },
        }

    def __repr__(self) -> str:
        counts = ", ".join(
            f"{name}={len(bucket.seeds)}" for name, bucket in self._buckets.items()
        )
        return (
            f"TargetSeedSet(family={self.family!r}, dataset_id={self.dataset_id!r}, "
            f"total_seeds={len(self.seeds)}, buckets={{ {counts} }})"
        )

    def __str__(self) -> str:
        return self.__repr__()


class SeedCorpus:
    def __init__(
        self,
        targets: dict[str, TargetSeedSet],
        aliases: dict[str, str] | None = None,
        target_groups: dict[str, tuple[str, ...]] | None = None,
        manifest_path: Path | None = None,
    ) -> None:
        self._targets = targets
        self._aliases = {**DEFAULT_TARGET_ALIASES, **(aliases or {})}
        self._target_groups = {
            **DEFAULT_TARGET_GROUPS, **(target_groups or {})}
        self.manifest_path = manifest_path

    @classmethod
    def load(
        cls,
        corpus_dir: str | Path = DEFAULT_SEED_CORPUS_DIR,
        manifest_name: str = "manifest.json",
        aliases: dict[str, str] | None = None,
        target_groups: dict[str, tuple[str, ...]] | None = None,
    ) -> "SeedCorpus":
        corpus_dir = Path(corpus_dir)
        manifest_path = corpus_dir / manifest_name
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        targets_cfg = manifest.get("targets", {})

        targets: dict[str, TargetSeedSet] = {}
        for family, rel_path in targets_cfg.items():
            file_path = corpus_dir / rel_path
            targets[family] = _load_target_seed_set(
                file_path, expected_family=family)

        return cls(
            targets=targets,
            aliases=aliases,
            target_groups=target_groups,
            manifest_path=manifest_path,
        )

    def families(self) -> tuple[str, ...]:
        return tuple(self._targets.keys())

    def resolve_family(self, target_or_family: str) -> str:
        family = self._aliases.get(target_or_family, target_or_family)
        if family not in self._targets:
            known = ", ".join(sorted(self._targets))
            raise KeyError(
                f"unknown target/family {target_or_family!r}; known families: {known}"
            )
        return family

    def target(self, target_or_family: str) -> TargetSeedSet:
        return self._targets[self.resolve_family(target_or_family)]

    def sample(
        self,
        target_or_family: str,
        rng: random.Random | None = None,
        bucket: str | None = None,
        bucket_weights: dict[str, float] | None = None,
    ) -> Seed:
        return self.target(target_or_family).sample(
            rng=rng,
            bucket=bucket,
            bucket_weights=bucket_weights,
        )

    def sample_ratio_batch(
        self,
        target_or_family: str,
        total: int,
        bucket_ratios: dict[str, float],
        rng: random.Random | None = None,
        *,
        shuffle: bool = True,
    ) -> list[Seed]:
        """
        Reusable batch sampler by target/family and total size.

        - Exact total size
        - Exact bucket counts from `bucket_ratios` (largest-remainder rounding)
        - No replacement (throws if capacity is insufficient)
        - For grouped targets (e.g. cidrize-runner), splits total evenly by family first
        """
        rng = rng or random.Random()

        if target_or_family in self._target_groups:
            return self._sample_ratio_batch_grouped(
                target_name=target_or_family,
                total=total,
                bucket_ratios=bucket_ratios,
                rng=rng,
                shuffle=shuffle,
            )

        seed_set = self.target(target_or_family)
        pools = {
            bucket_name: list(seed_set.bucket(bucket_name).seeds)
            for bucket_name in seed_set.buckets
        }
        counts = _plan_bucket_counts_from_ratios(
            total, bucket_ratios, set(pools))
        batch = _sample_from_bucket_pools(
            pools=pools,
            bucket_counts=counts,
            rng=rng,
            target_label=target_or_family,
        )
        if shuffle and len(batch) > 1:
            rng.shuffle(batch)
        return batch

    def _sample_ratio_batch_grouped(
        self,
        target_name: str,
        total: int,
        bucket_ratios: dict[str, float],
        rng: random.Random,
        shuffle: bool,
    ) -> list[Seed]:
        families = self._target_groups[target_name]
        family_totals = _split_total_evenly(total, len(families))
        global_bucket_counts = _plan_bucket_counts_from_ratios(
            total,
            bucket_ratios,
            set(self.target(families[0]).buckets),
        )

        remaining_bucket_counts = dict(global_bucket_counts)
        out: list[Seed] = []

        for i, family in enumerate(families):
            seed_set = self.target(family)
            pools = {
                bucket_name: list(seed_set.bucket(bucket_name).seeds)
                for bucket_name in seed_set.buckets
            }

            if i < len(families) - 1:
                counts = _plan_bucket_counts_from_ratios(
                    family_totals[i],
                    bucket_ratios,
                    set(pools),
                )
                for bucket_name, count in counts.items():
                    if count > remaining_bucket_counts.get(bucket_name, 0):
                        raise ValueError(
                            f"group allocation overflow for {target_name!r}: "
                            f"{family!r} requested {count} from {bucket_name!r}, "
                            f"but only {remaining_bucket_counts.get(bucket_name, 0)} "
                            "remaining after global bucket planning"
                        )
            else:
                counts = dict(remaining_bucket_counts)
                if sum(counts.values()) != family_totals[i]:
                    raise ValueError(
                        f"group allocation mismatch for {target_name!r}: last family "
                        f"{family!r} needs {family_totals[i]} total but remaining "
                        f"bucket counts sum to {sum(counts.values())}"
                    )

            family_batch = _sample_from_bucket_pools(
                pools=pools,
                bucket_counts=counts,
                rng=rng,
                target_label=f"{target_name}:{family}",
            )
            out.extend(family_batch)

            for bucket_name, count in counts.items():
                remaining_bucket_counts[bucket_name] -= count

        if any(v != 0 for v in remaining_bucket_counts.values()):
            raise ValueError(
                f"group allocation bug for {target_name!r}: leftover counts "
                f"{remaining_bucket_counts}"
            )

        if shuffle and len(out) > 1:
            rng.shuffle(out)
        return out

    def summary(self) -> dict[str, Any]:
        return {
            "manifest": str(self.manifest_path) if self.manifest_path else None,
            "families": {
                family: seed_set.summary() for family, seed_set in self._targets.items()
            },
        }


def _sample_from_bucket_pools(
    *,
    pools: dict[str, list[Seed]],
    bucket_counts: dict[str, int],
    rng: random.Random,
    target_label: str,
) -> list[Seed]:
    out: list[Seed] = []
    for bucket_name, count in bucket_counts.items():
        if count < 0:
            raise ValueError(f"bucket count must be >= 0 for {bucket_name!r}")
        pool = pools[bucket_name]
        if count > len(pool):
            raise ValueError(
                f"requested {count} seeds from bucket {bucket_name!r} for "
                f"{target_label!r}, but only {len(pool)} available"
            )
        if count:
            out.extend(rng.sample(pool, k=count))
    return out


def _split_total_evenly(total: int, n_parts: int) -> list[int]:
    if total < 0:
        raise ValueError("total must be >= 0")
    if n_parts <= 0:
        raise ValueError("n_parts must be > 0")
    base = total // n_parts
    remainder = total % n_parts
    return [base + (1 if i < remainder else 0) for i in range(n_parts)]


def _plan_bucket_counts_from_ratios(
    total: int,
    bucket_ratios: dict[str, float],
    known_buckets: set[str],
) -> dict[str, int]:
    if total < 0:
        raise ValueError("total must be >= 0")
    if not bucket_ratios:
        raise ValueError("bucket_ratios must not be empty")

    for bucket_name, ratio in bucket_ratios.items():
        if bucket_name not in known_buckets:
            raise KeyError(
                f"unknown bucket {bucket_name!r}; known buckets: {sorted(known_buckets)}"
            )
        if ratio < 0:
            raise ValueError(f"bucket ratio must be >= 0 for {bucket_name!r}")

    ratio_sum = sum(bucket_ratios.values())
    if ratio_sum <= 0:
        raise ValueError("sum of bucket ratios must be > 0")

    normalized = {k: v / ratio_sum for k, v in bucket_ratios.items()}
    raw = {k: normalized[k] * total for k in normalized}
    counts = {k: int(raw[k]) for k in raw}
    remainder = total - sum(counts.values())

    # Largest-remainder tie-breaks are deterministic.
    order = sorted(
        raw.keys(),
        key=lambda k: (raw[k] - counts[k], normalized[k], k),
        reverse=True,
    )
    for i in range(remainder):
        counts[order[i % len(order)]] += 1
    return counts


def _fingerprint_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _load_target_seed_set(file_path: Path, expected_family: str) -> TargetSeedSet:
    doc = json.loads(file_path.read_text(encoding="utf-8"))
    family = doc["target_family"]
    if family != expected_family:
        raise ValueError(
            f"target family mismatch in {file_path}: expected {expected_family!r}, "
            f"got {family!r}"
        )

    bucket_specs = {bucket["name"]                    : bucket for bucket in doc.get("buckets", [])}
    bucket_members: dict[str, list[Seed]] = {name: [] for name in bucket_specs}
    seen_ids: set[str] = set()

    for ordinal, seed_doc in enumerate(doc.get("seeds", [])):
        seed_id = seed_doc["id"]
        if seed_id in seen_ids:
            raise ValueError(f"duplicate seed id {seed_id!r} in {file_path}")
        seen_ids.add(seed_id)

        bucket_name = seed_doc["bucket"]
        if bucket_name not in bucket_members:
            raise ValueError(
                f"seed {seed_id!r} references unknown bucket {bucket_name!r} in {file_path}"
            )

        text = seed_doc["content"]
        text_bytes = text.encode("utf-8")
        bucket_members[bucket_name].append(
            Seed(
                seed_id=seed_id,
                family=family,
                bucket=bucket_name,
                label=seed_doc.get("label", seed_id),
                text=text,
                tags=tuple(seed_doc.get("tags", [])),
                expected=seed_doc.get("expected", "unknown"),
                ordinal=ordinal,
                fingerprint=seed_doc.get(
                    "fingerprint", _fingerprint_bytes(text_bytes)),
            )
        )

    buckets = {
        name: SeedBucket(
            name=name,
            description=spec.get("description", ""),
            seeds=tuple(bucket_members[name]),
        )
        for name, spec in bucket_specs.items()
    }

    return TargetSeedSet(
        family=family,
        dataset_id=doc.get("dataset_id", file_path.stem),
        buckets=buckets,
        metadata={
            "path": str(file_path),
            "schema_version": doc.get("schema_version"),
            "notes": doc.get("notes", ""),
        },
    )


def corpus_summary_text(corpus: SeedCorpus) -> str:
    lines: list[str] = []
    for family in corpus.families():
        info = corpus.target(family).summary()
        lines.append(
            f"{family}: total={info['total_seeds']} buckets={info['bucket_counts']}")
    return "\n".join(lines)
