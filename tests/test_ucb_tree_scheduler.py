from __future__ import annotations

from seed_corpus import Seed
from seed_scheduler.ucb_tree_scheduler import UCBTreeScheduler


def _seed(index: int, *, bucket: str = "valid", text: str = "x") -> Seed:
    return Seed(
        seed_id=f"seed-{index}",
        family="json",
        bucket=bucket,
        label=f"Seed {index}",
        text=text,
        tags=(),
        expected="",
        ordinal=index,
        fingerprint=f"fp-{index}",
    )


def test_update_rebuckets_item_using_fresh_signals() -> None:
    scheduler = UCBTreeScheduler()
    item = scheduler.add(
        _seed(1),
        metadata={"signals": {"coverage_key": "cov-A", "bug_key": "bug-A"}},
    )

    leased = scheduler.next()
    assert leased.item_id == item.item_id

    scheduler.update(
        leased,
        isinteresting_score=1.0,
        signals={"coverage_key": "cov-B", "bug_key": "bug-B", "new_coverage": True},
    )

    snapshot = scheduler.debug_dump(limit=10)
    leaves = {
        (leaf["coverage_key"], leaf["bug_key"]): leaf
        for leaf in snapshot["leaves"]
    }
    assert ("cov-B", "bug-B") in leaves
    assert ("cov-A", "bug-A") not in leaves
    assert leaves[("cov-B", "bug-B")]["seed_ids"] == ["seed-1"]


def test_leaf_overflow_prefers_new_seed_over_old_low_value_seed() -> None:
    scheduler = UCBTreeScheduler(max_seeds_per_leaf=2)
    first = scheduler.add(
        _seed(1),
        metadata={"signals": {"coverage_key": "cov", "bug_key": "bug"}},
    )
    second = scheduler.add(
        _seed(2),
        metadata={"signals": {"coverage_key": "cov", "bug_key": "bug"}},
    )

    leased = scheduler.next()
    scheduler.update(
        leased,
        isinteresting_score=0.0,
        signals={"coverage_key": "cov", "bug_key": "bug"},
    )

    third = scheduler.add(
        _seed(3),
        metadata={"signals": {"coverage_key": "cov", "bug_key": "bug"}},
    )

    snapshot = scheduler.debug_dump(limit=10)
    leaves = snapshot["leaves"]
    assert len(leaves) == 1
    kept_ids = set(leaves[0]["seed_ids"])

    assert third.seed.seed_id in kept_ids
    assert len(kept_ids) == 2
    assert first.seed.seed_id not in kept_ids or second.seed.seed_id not in kept_ids
