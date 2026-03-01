from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    return None


def _normalize_status(status: Any) -> str:
    if not isinstance(status, str):
        return ""
    return status.strip().lower()


def _bug_signatures_equal(a: Any, b: Any) -> bool:
    ma = _as_mapping(a)
    mb = _as_mapping(b)
    if ma is None or mb is None:
        return ma is None and mb is None

    keys = ("type", "exception", "message", "file", "line")
    return all(ma.get(k) == mb.get(k) for k in keys)


def _status_score(*, closed_status: str) -> float:
    """
    Basic per-run interestingness based only on the closed_result status.
    """
    if not closed_status:
        return 0.0

    if closed_status in {"bug", "crash"}:
        return 0.9
    if closed_status in {"timeout"}:
        return 0.7
    if closed_status in {"error"}:
        return 0.6

    return 0.0


def _differential_score(
    *,
    closed_status: str,
    open_status: str | None,
    closed_bug: Mapping[str, Any] | None,
    open_bug: Mapping[str, Any] | None,
) -> float:
    """
    Score based on differences between closed and open (oracle) behavior.
    """
    if not open_status and not open_bug:
        return 0.0

    if open_status is None:
        open_status = ""

    # Strong signal: closed finds a problem while the oracle looks fine.
    if closed_status in {"bug", "crash", "timeout", "error"} and open_status == "ok":
        return 1.0

    # Status differs in any other way: still interesting but slightly less.
    if closed_status != open_status:
        return 0.75

    # Same status; check whether the detailed bug signatures disagree.
    if closed_status in {"bug", "crash", "error"} and not _bug_signatures_equal(
        closed_bug, open_bug
    ):
        return 0.5

    return 0.0


def _coverage_score(
    *,
    covered_branches: int | None,
    missing_branches: int | None,
) -> float:
    """
    Compute a simple coverage-based score from aggregate branch counts.
    """
    if covered_branches is None or missing_branches is None:
        return 0.0

    try:
        covered = int(covered_branches)
        missing = int(missing_branches)
    except (TypeError, ValueError):
        return 0.0

    if covered < 0 or missing < 0:
        return 0.0

    total = covered + missing
    if total <= 0:
        return 0.0

    ratio = covered / float(total)
    # Prefer inputs that execute more of the available branches.
    return max(0.0, min(ratio, 1.0))


def compute_interestingness(*, result: Mapping[str, Any]) -> float:
    """
    Compute an "interestingness" score in [0.0, 1.0] for a single fuzzing input.

    The input is expected to be the top-level dictionary returned by
    parser.run_parser(...), i.e. something like:

        {
            "closed_result": {...},
            "open_result": {...}  # optional oracle result
        }

    The primary signal comes from closed_result. open_result is treated purely
    as an oracle for differential behavior. Coverage-related fields are
    optional and only used when present:

        closed_result["covered_branches"]         -> int (optional)
        closed_result["missing_branches"]         -> int (optional)
        closed_result["branch_details_by_file"]   -> list[...] (optional, unused)
    """
    top = _as_mapping(result)
    if top is None:
        return 0.0

    closed_raw = top.get("closed_result")
    closed = _as_mapping(closed_raw)
    if closed is None:
        return 0.0

    open_raw = top.get("open_result")
    open_res = _as_mapping(open_raw) if open_raw is not None else None

    closed_status = _normalize_status(closed.get("status"))
    open_status = _normalize_status(open_res.get("status")) if open_res else None

    closed_bug = _as_mapping(closed.get("bug_signature"))
    open_bug = _as_mapping(open_res.get("bug_signature")) if open_res else None

    covered_branches = closed.get("covered_branches")
    missing_branches = closed.get("missing_branches")

    s_status = _status_score(closed_status=closed_status)
    s_diff = _differential_score(
        closed_status=closed_status,
        open_status=open_status,
        closed_bug=closed_bug,
        open_bug=open_bug,
    )
    s_cov = _coverage_score(
        covered_branches=covered_branches,
        missing_branches=missing_branches,
    )

    # Combine signals conservatively: we care about any strong signal of
    # interesting behavior, so take the maximum of the individual scores.
    score = max(s_status, s_diff, s_cov, 0.0)
    if score < 0.0:
        return 0.0
    if score > 1.0:
        return 1.0
    return float(score)
