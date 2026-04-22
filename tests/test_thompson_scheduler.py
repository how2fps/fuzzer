from __future__ import annotations

from seed_corpus import Seed
from seed_scheduler import ThompsonFeatureScheduler


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


def test_bootstrap_add_registers_flat_feature_hints() -> None:
    scheduler = ThompsonFeatureScheduler(rng_seed=123)
    scheduler.add(
        _seed(1),
        metadata={"signals": {"coverage_key": {"family": "json", "bucket": "valid"}}},
    )

    snapshot = scheduler.debug_dump(limit=10)
    assert snapshot["stats"]["features"] == 1
    assert snapshot["features"][0]["favored_seed_id"] == "seed-1"


def test_update_uses_covered_edges_as_features_and_increments_alpha() -> None:
    scheduler = ThompsonFeatureScheduler(rng_seed=123)
    item = scheduler.add(_seed(1))
    leased = scheduler.next()
    assert leased.item_id == item.item_id

    scheduler.update(
        leased,
        isinteresting_score=1.0,
        signals={
            "new_coverage": True,
            "closed_result": {
                "status": "ok",
                "branch_details_by_file": [
                    {
                        "file": "decoder.py",
                        "covered_branches": [{"from_line": 10, "to_line": 12}],
                    }
                ],
            },
        },
    )

    snapshot = scheduler.debug_dump(limit=10)
    features = {row["feature_id"]: row for row in snapshot["features"]}
    assert "edge:decoder.py:10:12" in features
    assert features["edge:decoder.py:10:12"]["alpha"] == 2.0
    assert features["edge:decoder.py:10:12"]["beta"] == 1.0


def test_update_uses_differential_fallback_for_black_box_targets() -> None:
    scheduler = ThompsonFeatureScheduler(rng_seed=123)
    item = scheduler.add(_seed(1))
    leased = scheduler.next()

    scheduler.update(
        leased,
        isinteresting_score=0.0,
        signals={
            "new_differential_behavior": True,
            "closed_result": {
                "status": "ok",
                "stdout_signature": "target-hash",
            },
            "open_result": {
                "status": "ok",
                "stdout_signature": "oracle-hash",
            },
        },
    )

    snapshot = scheduler.debug_dump(limit=10)
    feature_ids = {row["feature_id"] for row in snapshot["features"]}
    assert any(feature_id.startswith("diff:") for feature_id in feature_ids)
