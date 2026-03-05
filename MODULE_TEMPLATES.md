# Module version templates

Templates for adding new versions to each fuzzer module (for ablation tests).

## Quick reference

| Module            | Template path                      | Register in                    |
|-------------------|------------------------------------|--------------------------------|
| isinteresting     | `isinteresting/versions/_template.py`  | `versions/__init__.py` REGISTRY |
| mutator           | `mutator/versions/_template.py`       | `versions/__init__.py` REGISTRY |
| parser            | `parser/versions/_template.py`        | `versions/__init__.py` REGISTRY |
| power_scheduler   | `power_scheduler/versions/_template.py`| `versions/__init__.py` REGISTRY |
| seed_corpus       | `seed_corpus/versions/_template.py`   | `versions/__init__.py` REGISTRY |
| seed_scheduler    | `seed_scheduler/_template_scheduler.py`| `seed_scheduler/__init__.py` (make_scheduler + SCHEDULER_KINDS) |

## Steps to add a new version

1. Copy the `_template.py` (or `_template_scheduler.py`) to a new file, e.g. `my_version.py`.
2. Implement the required interface (see template docstring and comments).
3. Register the new version:
   - **isinteresting / mutator / parser / power_scheduler / seed_corpus**: In that module’s `versions/__init__.py`, add an import and a `REGISTRY["my_version"] = ...` entry.
   - **seed_scheduler**: In `seed_scheduler/__init__.py`, import your class, add its kind(s) to `make_scheduler()`, and extend `SCHEDULER_KINDS` if desired.
4. Run with e.g. `python main.py --isinteresting-version my_version` (or the corresponding `--*-version` / `--scheduler` flag).

## Interfaces at a glance

- **isinteresting**: `compute_interestingness(*, result: Mapping) -> float` (0.0–1.0).
- **mutator**: `mutate(text, *, mutator_kind, rng) -> str`.
- **parser**: Module with `run_parser(...)`, `TARGETS`, `DEFAULT_TIMEOUT`.
- **power_scheduler**: Module with `compute_power_schedule(*, seeds, config?)` returning `PowerScheduleResult`.
- **seed_corpus**: Class with `load()` classmethod returning a corpus instance (same interface as `SeedCorpus`).
- **seed_scheduler**: Class inheriting `BaseSeedScheduler` and implementing `add`, `next`, `update`, `empty`, `__len__`, `stats`.
