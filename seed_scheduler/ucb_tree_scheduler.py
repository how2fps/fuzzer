from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from isinteresting import get_covered_edges_from_result
from seed_corpus import Seed

from core.fuzzer_logging import get_fuzzer_logger
from core.sqlite_conn import open_results_db

from .base import BaseSeedScheduler
from .types import ScheduledSeed


def _short_hash(obj: Any) -> str:
    """Return a stable short hash for bucketing complex scheduler signal payloads."""
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8", errors="replace"
    )
    return hashlib.sha256(raw).hexdigest()[:16]


def _merge_line_ranges(values: list[int]) -> list[str]:
    """Merge sorted line numbers into compact inclusive ranges."""
    if not values:
        return []
    ordered = sorted(set(v for v in values if isinstance(v, int) and v > 0))
    if not ordered:
        return []
    ranges: list[str] = []
    start = ordered[0]
    end = ordered[0]
    for value in ordered[1:]:
        if value <= end + 1:
            end = value
            continue
        ranges.append(f"{start}-{end}" if start != end else str(start))
        start = value
        end = value
    ranges.append(f"{start}-{end}" if start != end else str(start))
    return ranges


def _summarize_branch_ranges(branch_details_by_file: Any) -> dict[str, dict[str, list[str]]]:
    """Summarize branch details as merged line ranges per file."""
    if not isinstance(branch_details_by_file, list):
        return {}
    summary: dict[str, dict[str, list[str]]] = {}
    for file_entry in branch_details_by_file:
        if not isinstance(file_entry, dict):
            continue
        file_name = file_entry.get("file")
        if not isinstance(file_name, str) or not file_name:
            continue
        covered_lines: list[int] = []
        missing_lines: list[int] = []
        for arc in file_entry.get("covered_branches", []):
            if isinstance(arc, dict):
                for key in ("from_line", "to_line"):
                    value = arc.get(key)
                    if isinstance(value, int) and value > 0:
                        covered_lines.append(value)
        for arc in file_entry.get("missing_branches", []):
            if isinstance(arc, dict):
                for key in ("from_line", "to_line"):
                    value = arc.get(key)
                    if isinstance(value, int) and value > 0:
                        missing_lines.append(value)
        summary[file_name] = {
            "covered": _merge_line_ranges(covered_lines),
            "missing": _merge_line_ranges(missing_lines),
        }
    return summary


def _summarize_trace_payload(
    *,
    raw_signals: dict[str, Any] | None,
    normalized_signals: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build a compact, human-readable summary of one UCB update payload."""
    closed = raw_signals.get("closed_result", {}) if isinstance(raw_signals, dict) else {}
    bug_signature = normalized_signals.get("bug_signature") if normalized_signals else None
    covered = closed.get("covered_branches")
    missing = closed.get("missing_branches")
    total = None
    if isinstance(covered, int) and isinstance(missing, int):
        total = covered + missing
    branch_ranges = _summarize_branch_ranges(closed.get("branch_details_by_file"))
    summary: dict[str, Any] = {
        "status": normalized_signals.get("status") if normalized_signals else None,
        "bug_type": bug_signature.get("type") if isinstance(bug_signature, dict) else None,
        "new_coverage": normalized_signals.get("new_coverage") if normalized_signals else None,
        "new_bug": normalized_signals.get("new_bug") if normalized_signals else None,
        "crash": normalized_signals.get("crash") if normalized_signals else None,
        "timeout": normalized_signals.get("timeout") if normalized_signals else None,
        "covered_branches": covered,
        "missing_branches": missing,
        "total_branches": total,
        "branch_ranges": branch_ranges,
    }
    return {key: value for key, value in summary.items() if value is not None}


def _compact_coverage_key(key: Any) -> str:
    """Render coverage bucket keys in a readable one-line form."""
    if isinstance(key, dict):
        if "family" in key or "bucket" in key:
            family = key.get("family", "?")
            bucket = key.get("bucket", "?")
            return f"family={family} bucket={bucket}"
        if "branch_details_by_file" in key:
            branch_ranges = _summarize_branch_ranges(key.get("branch_details_by_file"))
            parts: list[str] = []
            for file_name, ranges in list(branch_ranges.items())[:2]:
                short_file = file_name.rsplit("/", 1)[-1]
                covered = ", ".join(ranges.get("covered", [])[:2]) or "-"
                parts.append(f"{short_file}:{covered}")
            if len(branch_ranges) > 2:
                parts.append("...")
            return "ranges=" + " | ".join(parts)
        return json.dumps(key, sort_keys=True, default=str)[:120]
    return str(key)


def _closed_status(result: dict[str, Any]) -> str:
    """Return normalized closed_result status."""
    closed = result.get("closed_result", {})
    status = closed.get("status")
    return str(status).strip().lower() if isinstance(status, str) else ""


def _has_new_coverage(
    db_path: Path | str,
    result: dict[str, Any],
    *,
    sqlite_conn: sqlite3.Connection | None = None,
) -> bool:
    """Return True if the current result covers any edge not yet in seen_branches."""
    edges = get_covered_edges_from_result(result)
    if not edges:
        return False

    def _any_new(conn: sqlite3.Connection) -> bool:
        try:
            for f, fl, tl in edges:
                row = conn.execute(
                    "SELECT 1 FROM seen_branches WHERE file = ? AND from_line = ? AND to_line = ? LIMIT 1",
                    (f, fl, tl),
                ).fetchone()
                if row is None:
                    return True
            return False
        except sqlite3.OperationalError:
            return False

    if sqlite_conn is not None:
        return _any_new(sqlite_conn)

    path = Path(db_path) if isinstance(db_path, str) else db_path
    if not path.exists():
        return False
    try:
        conn = open_results_db(path)
        try:
            return _any_new(conn)
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return False


def _has_new_bug(
    db_path: Path | str,
    result: dict[str, Any],
    target: str,
    *,
    sqlite_conn: sqlite3.Connection | None = None,
) -> bool:
    """Return True if the current bug/crash signature has not appeared before for this target."""
    status = _closed_status(result)
    if status not in {"bug", "crash", "timeout", "error"}:
        return False
    closed = result.get("closed_result", {})
    bug_signature = closed.get("bug_signature") or {}
    if not isinstance(bug_signature, dict):
        return False
    exc = bug_signature.get("exception") or ""
    file_ = bug_signature.get("file") or ""
    line_raw = bug_signature.get("line")
    line = None
    if line_raw is not None:
        try:
            line = int(line_raw)
        except (TypeError, ValueError):
            line = None

    def _is_first_occurrence(conn: sqlite3.Connection) -> bool:
        cur = conn.execute(
            """
            SELECT COUNT(*) FROM runs
            WHERE target = ? AND status IN ('bug', 'crash', 'timeout', 'error')
              AND COALESCE(exception, '') = COALESCE(?, '')
              AND COALESCE(file, '') = COALESCE(?, '')
              AND ((line IS NOT NULL AND line = ?) OR (line IS NULL AND ? IS NULL))
            """,
            (target, exc, file_, line, line),
        )
        row = cur.fetchone()
        return int(row[0]) == 0 if row else False

    if sqlite_conn is not None:
        try:
            return _is_first_occurrence(sqlite_conn)
        except sqlite3.OperationalError:
            return False

    path = Path(db_path) if isinstance(db_path, str) else db_path
    if not path.exists():
        return False
    try:
        conn = open_results_db(path)
        try:
            return _is_first_occurrence(conn)
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return False


def build_ucb_update_signals(
    *,
    result: dict[str, Any],
    db_path: Path | str,
    target: str,
    bucket: str,
    iteration: int,
    seed_id: str,
    score: float,
    sqlite_conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Build the per-mutation feedback payload consumed by the UCB scheduler."""
    status = _closed_status(result)
    return {
        "iteration": iteration,
        "seed_id": seed_id,
        "bucket": bucket,
        "status": status,
        "isinteresting": score,
        "new_coverage": _has_new_coverage(
            db_path, result, sqlite_conn=sqlite_conn
        ),
        "new_bug": _has_new_bug(db_path, result, target, sqlite_conn=sqlite_conn),
        "crash": status == "crash",
        "timeout": status == "timeout",
        "closed_result": result.get("closed_result", {}),
        "open_result": result.get("open_result", {}),
    }


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
        item.metadata["_ucb_insert_seq"] = self._seq
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

        if normalized_signals:
            cov_key = self._coverage_bucket_key(normalized_signals)
            bug_key = self._bug_bucket_key(normalized_signals)
        else:
            cov_key, bug_key = stored.metadata.get("_ucb_last_leaf") or stored.metadata.get(
                "_ucb_home", ("NO_COVERAGE", "NO_BUG")
            )
        stored.metadata["_ucb_home"] = (cov_key, bug_key)
        stored.metadata["_ucb_last_leaf"] = (cov_key, bug_key)
        stored.metadata.pop("_ucb_last_path", None)
        trace = stored.metadata.get("_ucb_trace")
        if trace:
            trace_summary = _summarize_trace_payload(
                raw_signals=signals,
                normalized_signals=normalized_signals,
            )
            get_fuzzer_logger().info(
                "[ucb.update] item=%s seed=%s score=%.3f reward=%.3f leaf=(%s, %s) summary=%r",
                stored.item_id,
                stored.seed.seed_id,
                isinteresting_score,
                reward,
                cov_key,
                bug_key,
                trace_summary,
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

    def supports_feedback_updates(self) -> bool:
        return True

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
                f"|- cov {_compact_coverage_key(cov_node.key)} N={cov_node.n_selected} Q={cov_node.q_avg_reward:.3f}"
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
            leaf.seeds.sort(key=self._leaf_retention_key, reverse=True)
            evicted = leaf.seeds[self._max_seeds_per_leaf:]
            leaf.seeds = leaf.seeds[: self._max_seeds_per_leaf]
            if leaf.rr_index > len(leaf.seeds):
                leaf.rr_index = len(leaf.seeds)
            for old in evicted:
                # If the just-added item gets evicted, also drop it from item registry.
                self._items.pop(old.item_id, None)

    def _leaf_retention_key(self, item: ScheduledSeed) -> tuple[float, float, float, float]:
        """
        Rank items to keep when a leaf overflows.

        Prefer unseen seeds first so new additions get evaluated at least once,
        then prefer historically higher-value seeds, then less-selected seeds,
        and finally newer arrivals as a deterministic tiebreaker.
        """
        is_unseen = 1.0 if item.updates == 0 else 0.0
        avg_score = item.avg_isinteresting_score
        less_selected = -float(item.times_selected)
        insert_seq = float(item.metadata.get("_ucb_insert_seq", 0))
        return (is_unseen, avg_score, less_selected, insert_seq)

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
