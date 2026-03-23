from __future__ import annotations

import hashlib
import json
import random
from typing import Any

from seed_corpus import Seed

from .base import BaseSeedScheduler
from .types import ScheduledSeed


def _stable_hash(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def _covered_edge_features(closed: dict[str, Any]) -> list[str]:
    details = closed.get("branch_details_by_file")
    if not isinstance(details, list):
        return []
    features: list[str] = []
    for file_entry in details:
        if not isinstance(file_entry, dict):
            continue
        file_name = file_entry.get("file")
        if not isinstance(file_name, str) or not file_name:
            continue
        for arc in file_entry.get("covered_branches", []):
            if not isinstance(arc, dict):
                continue
            from_line = arc.get("from_line")
            to_line = arc.get("to_line")
            if isinstance(from_line, int) and from_line > 0 and isinstance(to_line, int):
                features.append(f"edge:{file_name}:{from_line}:{to_line}")
    return features


def _bug_signature_feature(closed: dict[str, Any]) -> str | None:
    bug_signature = closed.get("bug_signature")
    if not isinstance(bug_signature, dict):
        return None
    meaningful = {
        key: value
        for key, value in bug_signature.items()
        if value not in (None, "", [], {})
    }
    if not meaningful:
        return None
    return "bug:" + _stable_hash(meaningful)


def _differential_feature(signals: dict[str, Any]) -> str | None:
    closed = signals.get("closed_result")
    open_ = signals.get("open_result")
    if not isinstance(closed, dict) or not isinstance(open_, dict):
        return None

    tuple_payload = {
        "closed_status": closed.get("status"),
        "closed_stdout_signature": closed.get("stdout_signature"),
        "closed_stderr_signature": closed.get("stderr_signature"),
        "open_status": open_.get("status"),
        "open_stdout_signature": open_.get("stdout_signature"),
        "open_stderr_signature": open_.get("stderr_signature"),
        "closed_bug_signature": closed.get("bug_signature"),
        "open_bug_signature": open_.get("bug_signature"),
    }
    if not any(value not in (None, "", [], {}) for value in tuple_payload.values()):
        return None
    return "diff:" + _stable_hash(tuple_payload)


def _status_feature(closed: dict[str, Any]) -> str:
    status = closed.get("status")
    return f"status:{str(status).strip().lower() or 'unknown'}"


def _extract_feature_ids(signals: dict[str, Any] | None) -> list[str]:
    """
    Extract feature ids from one execution result.

    Preference order:
    1. explicit `feature_ids`
    2. coverage edges (grey-box targets)
    3. differential behavior tuple (oracle-backed targets)
    4. bug signature
    5. coarse status fallback
    """
    if not isinstance(signals, dict):
        return []
    explicit = signals.get("feature_ids")
    if isinstance(explicit, list) and explicit:
        return [str(feature_id) for feature_id in explicit]

    flat_features: list[str] = []
    coverage_key = signals.get("coverage_key")
    if coverage_key not in (None, "", [], {}):
        flat_features.append("coverage-key:" + _stable_hash(coverage_key))
    bug_key = signals.get("bug_key")
    if bug_key not in (None, "", [], {}):
        flat_features.append("bug-key:" + str(bug_key))
    if flat_features:
        return flat_features

    closed = signals.get("closed_result")
    if not isinstance(closed, dict):
        return []

    features = _covered_edge_features(closed)
    if features:
        return features

    differential_feature = _differential_feature(signals)
    if differential_feature is not None:
        features.append(differential_feature)

    bug_feature = _bug_signature_feature(closed)
    if bug_feature is not None:
        features.append(bug_feature)

    if features:
        return features
    return [_status_feature(closed)]


def _is_success(
    *,
    isinteresting_score: float,
    signals: dict[str, Any] | None,
) -> bool:
    """
    Binary reward for Thompson Sampling.

    Success is a novel result:
    - new coverage
    - new bug
    - new differential behavior

    As a compatibility fallback for older result payloads, a positive
    interestingness score is also treated as success when no explicit novelty
    flag is present.
    """
    if isinstance(signals, dict):
        novelty_flags = (
            bool(signals.get("new_coverage")),
            bool(signals.get("new_bug")),
            bool(signals.get("new_differential_behavior")),
        )
        if any(novelty_flags):
            return True
        if any(flag_key in signals for flag_key in ("new_coverage", "new_bug", "new_differential_behavior")):
            return False
    return bool(isinteresting_score > 0)


class ThompsonFeatureScheduler(BaseSeedScheduler):
    """
    Thompson Sampling scheduler over target-dependent execution features.

    For coverage-aware targets, features are covered edges.
    For oracle / black-box targets, features fall back to differential-output
    tuples, bug signatures, and finally coarse status classes.
    """

    def __init__(self, *, rng_seed: int | None = None) -> None:
        self._rng = random.Random(rng_seed)
        self._items: dict[str, ScheduledSeed] = {}
        self._ready: set[str] = set()
        self._seq = 0
        self._feature_map: dict[str, dict[str, float]] = {}
        self._favored_inputs: dict[str, str] = {}

    def add(self, seed: Seed, *, metadata: dict[str, Any] | None = None) -> ScheduledSeed:
        self._seq += 1
        item_id = f"ts{self._seq:06d}"
        item = ScheduledSeed(
            item_id=item_id,
            seed=seed,
            priority=0.0,
            metadata=dict(metadata or {}),
        )
        self._items[item_id] = item
        self._ready.add(item_id)

        for feature_id in _extract_feature_ids(item.metadata.get("signals")):
            self._ensure_feature(feature_id)
            self._maybe_favor(feature_id, item)
        return item

    def next(self) -> ScheduledSeed:
        if not self._ready:
            raise IndexError("scheduler is empty")

        best_item_id: str | None = None
        best_feature: str | None = None
        best_score = -1.0

        for feature_id, params in self._feature_map.items():
            item_id = self._favored_inputs.get(feature_id)
            if item_id not in self._ready:
                continue
            alpha = max(float(params.get("alpha", 1.0)), 1e-6)
            beta = max(float(params.get("beta", 1.0)), 1e-6)
            theta = self._rng.betavariate(alpha, beta)
            psi = self._rng.betavariate(alpha + beta, alpha * alpha)
            score = theta * psi
            if score > best_score:
                best_score = score
                best_item_id = item_id
                best_feature = feature_id

        if best_item_id is None:
            # Bootstrap / fallback path when no features are registered yet.
            best_item_id = min(self._ready)
            best_feature = None

        self._ready.remove(best_item_id)
        item = self._items[best_item_id]
        item.times_selected += 1
        item.metadata["_ts_last_feature"] = best_feature
        item.priority = best_score if best_score >= 0.0 else 0.0
        return item

    def update(
        self,
        item: ScheduledSeed,
        *,
        isinteresting_score: float,
        signals: dict[str, Any] | None = None,
    ) -> ScheduledSeed:
        if item.item_id not in self._items:
            raise KeyError(f"unknown item_id {item.item_id!r}")

        stored = self._items[item.item_id]
        stored.last_isinteresting_score = float(isinteresting_score)
        stored.total_isinteresting_score += float(isinteresting_score)
        stored.updates += 1

        feature_ids = _extract_feature_ids(signals)
        success = _is_success(
            isinteresting_score=isinteresting_score,
            signals=signals,
        )
        for feature_id in feature_ids:
            params = self._ensure_feature(feature_id)
            if success:
                params["alpha"] += 1.0
            else:
                params["beta"] += 1.0
            self._maybe_favor(feature_id, stored)

        if signals:
            stored.metadata["last_signals"] = signals
        stored.metadata["_ts_last_feature_ids"] = feature_ids
        self._ready.add(stored.item_id)
        return stored

    def empty(self) -> bool:
        return len(self._ready) == 0

    def __len__(self) -> int:
        return len(self._ready)

    def stats(self) -> dict[str, Any]:
        return {
            "kind": "thompson",
            "ready": len(self._ready),
            "total_items": len(self._items),
            "features": len(self._feature_map),
            "favored_inputs": len(self._favored_inputs),
        }

    def debug_dump(self, limit: int = 20) -> dict[str, Any]:
        ready_items = [
            {
                "item_id": item.item_id,
                "seed_id": item.seed.seed_id,
                "bucket": item.seed.bucket,
                "times_selected": item.times_selected,
                "avg_isinteresting_score": item.avg_isinteresting_score,
                "last_feature_ids": item.metadata.get("_ts_last_feature_ids", []),
            }
            for item_id, item in sorted(self._items.items())
            if item_id in self._ready
        ][: max(limit, 0)]
        feature_rows = [
            {
                "feature_id": feature_id,
                "alpha": round(params["alpha"], 3),
                "beta": round(params["beta"], 3),
                "favored_seed_id": self._items[self._favored_inputs[feature_id]].seed.seed_id
                if self._favored_inputs.get(feature_id) in self._items
                else None,
            }
            for feature_id, params in sorted(self._feature_map.items())
        ][: max(limit, 0)]
        return {
            "stats": self.stats(),
            "ready_items": ready_items,
            "features": feature_rows,
            "truncated": len(self._feature_map) > len(feature_rows),
        }

    def supports_feedback_updates(self) -> bool:
        return True

    def _ensure_feature(self, feature_id: str) -> dict[str, float]:
        return self._feature_map.setdefault(feature_id, {"alpha": 1.0, "beta": 1.0})

    def _maybe_favor(self, feature_id: str, item: ScheduledSeed) -> None:
        current_item_id = self._favored_inputs.get(feature_id)
        if current_item_id is None:
            self._favored_inputs[feature_id] = item.item_id
            return
        current = self._items.get(current_item_id)
        if current is None or self._favor_key(item) < self._favor_key(current):
            self._favored_inputs[feature_id] = item.item_id

    def _favor_key(self, item: ScheduledSeed) -> tuple[int, int, str]:
        return (len(item.seed.text), item.times_selected, item.item_id)
