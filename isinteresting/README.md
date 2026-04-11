# Interestingness Scoring Formula

This note summarizes the `base` interestingness function implemented in
[`isinteresting/versions/base.py`](/home/fuzzer/fuzzer/isinteresting/versions/base.py).

## Overall Score

The final interestingness score is clipped to the interval `[0, 1]`:

```text
I = clip(Score, 0, 1)
```

First compute a normalized weighted score:

```text
WeightedScore = M / M_max
```

where:

```text
M =
  ((s_status + s_diff) * r_bug)
  + (s_cov * c_f)
  + (2.0 * s_new * c_f)
  + (0.9 * s_rare)
  + (1.2 * s_site)
  + (0.7 * s_excsite)
```

If database-backed novelty terms are available:

```text
M_max = 2.0 + I_cov + 2.0 + 0.9 + 1.2 + 0.7
```

Otherwise:

```text
M_max = 2.0 + I_cov
```

where `I_cov = 1` if coverage counts are present, else `0`.

The final score is then:

```text
if an oracle/open result is present:
    Score = 1.0 if s_new > 0 else 0.0
else:
    Score = s_new + (1 - s_new) * WeightedScore
```

## Component Definitions

### Bug Repeat Damping

```text
r_bug = 1 / (1 + repeatBugCount)
```

This reduces the effect of status and differential signals when the same bug has
already been seen many times.

### Coverage Factor

If coverage comes from the closed run:

```text
c_f = 1
```

If an oracle/open result is present, the final score ignores all other signals:

```text
Score =
    1.0  if the run discovers at least one new covered edge
    0.0  otherwise
```

This makes oracle-backed targets strictly coverage-novelty driven.

### Status Score

```text
s_status =
    0.9  if status in {bug, crash}
    0.7  if status == timeout
    0.6  if status == error
    0.0  otherwise
```

### Differential Score

```text
s_diff =
    1.0   if closed is bug/crash/timeout/error and open is ok
    0.75  if closed and open statuses differ
    0.5   if statuses match but bug signatures differ
    0.0   otherwise
```

### Coverage Score

```text
s_cov = coveredBranches / (coveredBranches + missingBranches)
```

If the counts are missing or invalid, this term is `0`.

### Rare Bug Terms

```text
s_rare    = 1 / (1 + repeatBugCount)
s_site    = 1 / (1 + repeatBugSiteCount)
s_excsite = 1 / (1 + repeatExceptionSiteCount)
```

These reward bugs that are globally rare, rare at a code location, or rare for a
particular exception at a location.

## New-Edge Novelty Score

The new-edge novelty score is:

```text
if N_edges == 0:
    s_new = 0
else:
    s_new = 0.5 * min(N_new, 1) + 0.5 * (N_new / N_edges)
```

Since `min(N_new, 1)` is just an indicator for whether at least one new edge
exists, this can be written more simply as:

```text
s_new = 0.5 * I[N_new > 0] + 0.5 * (N_new / N_edges)
```

where:

- `N_edges` is the number of covered edges exercised by the input.
- `N_new` is the number of those edges not yet present in `seen_branches`.

Interpretation:

- The first half rewards discovering at least one new edge.
- The second half rewards the fraction of executed edges that are new.

Examples:

- No new edges: `0.5 * 0 + 0.5 * 0 = 0`
- 1 new edge out of 10: `0.5 + 0.5 * 0.1 = 0.55`
- 5 new edges out of 10: `0.5 + 0.5 * 0.5 = 0.75`
- All 10 edges are new: `0.5 + 0.5 * 1.0 = 1.0`

## Short Presentation Version

For slides, a compact summary is:

```text
Interestingness
  ~= status
   + differential mismatch
   + coverage
   + new-edge novelty
   + rare-bug bonuses
```

with normalization to `[0, 1]`, and repeated bugs damped by:

```text
1 / (1 + count)
```
