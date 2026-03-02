from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any

from seed_corpus import Seed

from .base import BaseSeedScheduler
from .types import ScheduledSeed


def _short_hash(obj: Any) -> str:
    """Return a stable short hash for bucketing complex scheduler signal payloads."""
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8", errors="replace"
    )
    return hashlib.sha256(raw).hexdigest()[:16]


@dataclass
class _TreeNode:
    """Tree node used to group scheduled items by coverage and bug buckets."""

    kind: str  # root | coverage | bug
    key: str
    parent: _TreeNode | None = None
    children: dict[str, _TreeNode] = field(default_factory=dict)
    seeds: list[ScheduledSeed] = field(default_factory=list)  # for bug nodes only
    n_selected: int = 0
    q_avg_reward: float = 0.0
    rr_index: int = 0

    def update_stats(self, reward: float) -> None:
        """Update running UCB reward statistics with a new observed reward."""
        self.n_selected += 1
        self.q_avg_reward += (reward - self.q_avg_reward) / self.n_selected


class UCBTreeScheduler(BaseSeedScheduler):
    """
    root -> coverage bucket -> bug/output bucket -> seeds

    UCB1 is used at each internal node to select the next child.
    Reward is computed from `signals` inside `update()` (Option A).
    """

    def __init__(self, *, ucb_c: float = 1.0, max_seeds_per_leaf: int = 8) -> None:
        """Initialize tree structure and UCB exploration parameters."""
        self._ucb_c = float(ucb_c)
        self._max_seeds_per_leaf = int(max_seeds_per_leaf)
        self._root = _TreeNode(kind="root", key="root")
        self._items: dict[str, ScheduledSeed] = {}
        self._seq = 0

    def add(self, seed: Seed, *, metadata: dict[str, Any] | None = None) -> ScheduledSeed:
        """Insert a seed into the coverage/bug leaf selected from its metadata signals."""
        metadata = dict(metadata or {})
        signals = self._normalize_signals(metadata.get("signals"))
        cov_key = self._coverage_bucket_key(signals)
        bug_key = self._bug_bucket_key(signals)
        leaf = self._ensure_leaf(cov_key, bug_key)

        self._seq += 1
        item = ScheduledSeed(
            item_id=f"u{self._seq:06d}",
            seed=seed,
            priority=0.0,
            metadata=metadata,
        )
        item.metadata["_ucb_home"] = (cov_key, bug_key)
        self._items[item.item_id] = item
        self._insert_into_leaf(leaf, item)
        return item

    def next(self) -> ScheduledSeed:
        """Traverse the tree with UCB1 and return one scheduled item from the chosen leaf."""
        if self.empty():
            raise IndexError("scheduler is empty")

        path = [self._root]
        node = self._root
        while node.kind != "bug":
            child = self._select_ucb_child(node)
            if child is None:
                raise IndexError("no selectable child")
            path.append(child)
            node = child

        if not node.seeds:
            raise IndexError("selected empty leaf")

        if node.rr_index >= len(node.seeds):
            node.rr_index = 0
        item = node.seeds.pop(node.rr_index)
        item.times_selected += 1
        item.metadata["_ucb_last_path"] = path
        item.metadata["_ucb_last_leaf"] = (path[-2].key, path[-1].key)
        return item

    def update(
        self,
        item: ScheduledSeed,
        *,
        isinteresting_score: float,
        signals: dict[str, Any] | None = None,
    ) -> ScheduledSeed:
        """Update rewards for the leased path and reinsert the item into its selected leaf."""
        if item.item_id not in self._items:
            raise KeyError(f"unknown item_id {item.item_id!r}")

        stored = self._items[item.item_id]
        stored.last_isinteresting_score = float(isinteresting_score)
        stored.total_isinteresting_score += float(isinteresting_score)
        stored.updates += 1
        normalized_signals = self._normalize_signals(signals)
        if normalized_signals:
            stored.metadata["last_signals"] = normalized_signals

        reward = self._reward_from_signals(normalized_signals)
        path = stored.metadata.get("_ucb_last_path")
        if not path:
            raise ValueError("update() called before next() for this item")
        for node in path:
            node.update_stats(reward)

        cov_key, bug_key = stored.metadata.get("_ucb_last_leaf") or stored.metadata.get(
            "_ucb_home", ("NO_COVERAGE", "NO_BUG")
        )
        leaf = self._ensure_leaf(cov_key, bug_key)
        self._insert_into_leaf(leaf, stored)
        return stored

    def empty(self) -> bool:
        """Return True when no leaf currently holds any schedulable items."""
        return self._available_count(self._root) == 0

    def __len__(self) -> int:
        """Return the number of ready items across all leaves."""
        return self._available_count(self._root)

    def stats(self) -> dict[str, Any]:
        """Return aggregate tree size and parameter metrics."""
        coverage_buckets = len(self._root.children)
        bug_buckets = sum(len(c.children) for c in self._root.children.values())
        return {
            "kind": "ucb_tree",
            "ready": len(self),
            "total_items": len(self._items),
            "coverage_buckets": coverage_buckets,
            "bug_buckets": bug_buckets,
            "ucb_c": self._ucb_c,
            "max_seeds_per_leaf": self._max_seeds_per_leaf,
        }

    def debug_dump(self, limit: int = 20) -> dict[str, Any]:
        """Return a leaf-oriented snapshot ordered by current average reward."""
        leaves: list[dict[str, Any]] = []
        for cov_key, cov_node in self._root.children.items():
            for bug_key, bug_node in cov_node.children.items():
                if not bug_node.seeds:
                    continue
                leaves.append(
                    {
                        "coverage_key": cov_key,
                        "bug_key": bug_key,
                        "leaf_n_selected": bug_node.n_selected,
                        "leaf_q_avg_reward": round(bug_node.q_avg_reward, 4),
                        "seed_count": len(bug_node.seeds),
                        "seed_ids": [s.seed.seed_id for s in bug_node.seeds[:5]],
                    }
                )
        # Surface the leaves with highest current Q first for a useful snapshot.
        leaves.sort(
            key=lambda x: (-x["leaf_q_avg_reward"], -x["leaf_n_selected"], x["coverage_key"], x["bug_key"])
        )
        return {
            "stats": self.stats(),
            "leaves": leaves[: max(limit, 0)],
            "truncated": len(leaves) > min(max(limit, 0), len(leaves)),
        }

    def render_tree(self, limit: int = 20) -> str:
        """Render a readable tree snapshot for logging/debugging."""
        lines = [
            "ucb_tree",
            (
                f"root ready={len(self)} total_items={len(self._items)} "
                f"coverage_buckets={len(self._root.children)} ucb_c={self._ucb_c}"
            ),
        ]
        emitted = 0
        coverage_nodes = sorted(
            self._root.children.values(),
            key=lambda node: (-node.q_avg_reward, -node.n_selected, node.key),
        )
        for cov_node in coverage_nodes:
            if emitted >= limit:
                break
            lines.append(
                f"|- cov {cov_node.key} N={cov_node.n_selected} Q={cov_node.q_avg_reward:.3f}"
            )
            bug_nodes = sorted(
                cov_node.children.values(),
                key=lambda node: (-node.q_avg_reward, -node.n_selected, node.key),
            )
            for bug_node in bug_nodes:
                if emitted >= limit:
                    break
                seed_ids = ", ".join(seed.seed.seed_id for seed in bug_node.seeds[:4])
                if len(bug_node.seeds) > 4:
                    seed_ids += ", ..."
                lines.append(
                    (
                        f"|  |- bug {bug_node.key} N={bug_node.n_selected} "
                        f"Q={bug_node.q_avg_reward:.3f} seeds={len(bug_node.seeds)} "
                        f"rr={bug_node.rr_index}"
                    )
                )
                if seed_ids:
                    lines.append(f"|  |  `- {seed_ids}")
                emitted += 1
        if emitted >= limit:
            lines.append("`- ...")
        return "\n".join(lines)

    def _ensure_leaf(self, cov_key: str, bug_key: str) -> _TreeNode:
        """Create or return the leaf node for a coverage/bug bucket pair."""
        cov = self._root.children.get(cov_key)
        if cov is None:
            cov = _TreeNode(kind="coverage", key=cov_key, parent=self._root)
            self._root.children[cov_key] = cov
        bug = cov.children.get(bug_key)
        if bug is None:
            bug = _TreeNode(kind="bug", key=bug_key, parent=cov)
            cov.children[bug_key] = bug
        return bug

    def _insert_into_leaf(self, leaf: _TreeNode, item: ScheduledSeed) -> None:
        """Insert an item into a leaf and evict overflow items beyond the leaf limit."""
        leaf.seeds.append(item)
        if len(leaf.seeds) > self._max_seeds_per_leaf:
            evicted = leaf.seeds[self._max_seeds_per_leaf :]
            leaf.seeds = leaf.seeds[: self._max_seeds_per_leaf]
            if leaf.rr_index > len(leaf.seeds):
                leaf.rr_index = len(leaf.seeds)
            for old in evicted:
                # If the just-added item gets evicted, also drop it from item registry.
                self._items.pop(old.item_id, None)

    def _select_ucb_child(self, parent: _TreeNode) -> _TreeNode | None:
        """Select the next child node to traverse using the UCB1 score."""
        candidates = [c for c in parent.children.values() if self._available_count(c) > 0]
        if not candidates:
            return None

        best = None
        best_score = -math.inf
        for child in candidates:
            score = self._ucb_score(parent, child)
            if score > best_score:
                best_score = score
                best = child
        return best

    def _ucb_score(self, parent: _TreeNode, child: _TreeNode) -> float:
        """Compute the UCB1 score for one child relative to its parent."""
        if child.n_selected == 0:
            return math.inf
        parent_n = max(parent.n_selected, 1)
        return child.q_avg_reward + self._ucb_c * math.sqrt(
            math.log(parent_n) / child.n_selected
        )

    def _available_count(self, node: _TreeNode) -> int:
        """Count schedulable items reachable from a node."""
        if node.kind == "bug":
            return len(node.seeds)
        return sum(self._available_count(child) for child in node.children.values())

    def _reward_from_signals(self, signals: dict[str, Any] | None) -> float:
        """Map execution signals into a scalar reward used by UCB updates."""
        if not signals:
            return 0.0
        reward = 0.0
        if bool(signals.get("new_coverage")):
            reward += 1.0
        if bool(signals.get("new_bug")):
            reward += 2.0
        status = str(signals.get("status", "")).lower()
        if bool(signals.get("crash")) or bool(signals.get("timeout")) or status in {
            "crash",
            "timeout",
        }:
            reward += 3.0
        return reward

    def _normalize_signals(self, signals: dict[str, Any] | None) -> dict[str, Any] | None:
        """
        Accept either a flat signals dict or a wrapped result shape:
          {"closed_result": {...}, "open_result": {...}}
        and normalize into the flat shape used by UCB bucketing/reward.
        """
        if not signals:
            return signals
        if not isinstance(signals, dict):
            return {"raw_signals": signals}

        # Already-flat shape.
        if "closed_result" not in signals and "open_result" not in signals:
            return signals

        closed = signals.get("closed_result") or {}
        open_ = signals.get("open_result") or {}

        status = str(closed.get("status") or open_.get("status") or "ok").lower()
        bug_signature = closed.get("bug_signature") or open_.get("bug_signature")

        out: dict[str, Any] = {
            "status": status,
            "bug_signature": bug_signature,
        }

        # Preserve explicit novelty flags if caller computed them.
        for key in ("new_coverage", "new_bug", "crash", "timeout"):
            if key in signals:
                out[key] = signals[key]
            elif key in closed:
                out[key] = closed[key]
            elif key in open_:
                out[key] = open_[key]

        # Coverage bucketing source (prefer explicit key/signature if provided).
        if signals.get("coverage_key"):
            out["coverage_key"] = signals["coverage_key"]
        elif closed.get("coverage_key"):
            out["coverage_key"] = closed["coverage_key"]
        elif closed.get("coverage_signature"):
            out["coverage_signature"] = closed["coverage_signature"]
        elif closed.get("branch_details_by_file") is not None:
            out["coverage_key"] = {"branch_details_by_file": closed.get("branch_details_by_file")}
        elif (
            "covered_branches" in closed
            or "missing_branches" in closed
            or "covered_branches" in open_
            or "missing_branches" in open_
        ):
            out["coverage_key"] = {
                "covered_branches": closed.get("covered_branches", open_.get("covered_branches")),
                "missing_branches": closed.get("missing_branches", open_.get("missing_branches")),
            }

        # Output signatures if present (for non-bug bucketing fallback).
        for key in ("stdout_signature", "stderr_signature", "semantic_output_signature"):
            if key in closed:
                out[key] = closed[key]
            elif key in open_:
                out[key] = open_[key]

        return out

    def _coverage_bucket_key(self, signals: dict[str, Any] | None) -> str:
        """Derive the coverage bucket key used for the first tree partition."""
        if not signals:
            return "NO_COVERAGE"
        if signals.get("coverage_key"):
            return str(signals["coverage_key"])
        if signals.get("coverage_signature"):
            return str(signals["coverage_signature"])
        if "coverage_bitmap" in signals and signals["coverage_bitmap"] is not None:
            return "COV:" + _short_hash(signals["coverage_bitmap"])
        return "NO_COVERAGE"

    def _bug_bucket_key(self, signals: dict[str, Any] | None) -> str:
        """Derive the bug/output bucket key used for the second tree partition."""
        if not signals:
            return "NO_BUG"
        if signals.get("bug_key"):
            return str(signals["bug_key"])

        bug_sig = signals.get("bug_signature")
        if isinstance(bug_sig, dict):
            meaningful = {k: v for k, v in bug_sig.items() if v not in (None, "", [], {})}
            if meaningful:
                return "BUG:" + _short_hash(meaningful)

        status = str(signals.get("status", "")).lower()
        if bool(signals.get("crash")) or bool(signals.get("timeout")) or status in {
            "crash",
            "timeout",
        }:
            return "BUG:CRASH_OR_TIMEOUT"

        if signals.get("stdout_signature") or signals.get("stderr_signature"):
            return "OUT:" + _short_hash(
                {
                    "stdout_signature": signals.get("stdout_signature"),
                    "stderr_signature": signals.get("stderr_signature"),
                }
            )
        return "NO_BUG"
