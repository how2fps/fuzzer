# BunnyCov

AFL-style fuzzer harness that wires a seed corpus, mutator, parser, interestingness scoring, and schedulers. Each run persists results to SQLite and exports CSVs under `results/`.

## How to run

From the project root (the directory containing `main.py`):

```bash
uv venv
source .venv/bin/activate
uv sync
```

Clone the target repositories into `targets/`:

```bash
mkdir -p targets
git clone https://github.com/TrustWare-Research-Group/IPv4-IPv6-parser targets/IPv4-IPv6-parser
git clone https://github.com/TrustWare-Research-Group/cidrize-runner targets/cidrize-runner
git clone https://github.com/TrustWare-Research-Group/json-decoder targets/json-decoder
git clone https://github.com/jathanism/cidrize targets/cidrize
git clone https://github.com/jcollie/ipyparse targets/ipyparse
```

Or, if you are already inside an environment with the project dependencies installed:

```bash
python main.py
```

With options:

```bash
python main.py --target json-decoder --iterations 5000 --seed 42
```

With the best configs:
```bash
uv run main.py --configs-dir configs/best_configs
```

You can always see the authoritative list of options and defaults with:

```bash
python main.py --help
```

### Command-line options

| Option | Default | Description |
|--------|---------|-------------|
| `--target` | `json-decoder` | Target name (must be a key in `parser.TARGETS`). Run `python main.py --help` to see available targets. |
| `--scheduler` | `heap` | Seed scheduler implementation. Choices come from `seed_scheduler.list_versions()`. |
| `--mutator` | `auto` | Mutation mode: `auto` (infer from target), `json`, or `ip`. |
| `--iterations` | `10` | Maximum number of fuzzing iterations (mutually exclusive with `--hours`). Use either `--iterations` or `--hours`, not both. |
| `--hours` | (none) | Maximum fuzzing time in hours (mutually exclusive with `--iterations`). |
| `--timeout` | `10.0` | Per-run timeout in seconds. |
| `--seed` | (none) | Optional RNG seed for reproducibility. |
| `--workers` | `1` | Number of worker processes. All workers share a single scheduler. |
| `--isinteresting-version` | `base` | Interestingness module version (for ablation). |
| `--mutator-version` | `base` | Mutator module version (for ablation). |
| `--parser-version` | `base` | Parser module version (for ablation). |
| `--power-scheduler-version` | `base` | Power scheduler module version (for ablation). |
| `--seed-corpus-version` | `base` | Seed corpus module version (for ablation). |
| `--config PATH` | (none) | Run a single JSON config file (see `configs/_template.json`). |
| `--configs-dir DIR` | (none) | Run all `.json` configs in `DIR` (non-recursive). Config files starting with `_` are ignored. |
| `--runs` | `1` | When using `--config` or `--configs-dir`, run each config this many times. |

### Examples

```bash
# Default: json-decoder, 10 iterations
python main.py

# More iterations, reproducible run
python main.py --iterations 10000 --seed 12345

# Fuzz another target
python main.py --target cidrize-runner --iterations 2000

# Run for a fixed amount of time instead of a fixed number of iterations
python main.py --target json-decoder --hours 2.0

# Shorter timeout
python main.py --timeout 5.0

# Multi-process fuzzing with 4 workers
python main.py --workers 4 --iterations 50000

# Run one config file
python main.py --config configs/my_run.json

# Run one config file 5 times
python main.py --config configs/my_run.json --runs 5

# Or store the repeat count in the config itself
# { "batch": { "runs": 5 }, ... }
python main.py --config configs/my_run.json

# Run all configs in a target folder (non-recursive), 5 times each
python main.py --configs-dir configs/best_configs --runs 5

# Run all configs from the local best_configs folder
python main.py --configs-dir /home/fuzzer/fuzzer/best_configs
```

## Results

### Single runs (CLI-only)

When running without `--config` / `--configs-dir`, a new folder is created under `results/` named:

```
results/<target>_<timestamp>/
```

For example: `results/json-decoder_20250301_143022/`

Contents:

- **`config.json`** — The config used for this run (resolved from CLI defaults + flags).
- **`runs.db`** — SQLite database of every iteration (seed id, seed text, mutated input, status, bug_type, exception, line, scores, etc.).
- **`runs.csv`** — Full export of `runs` as CSV.
- **`unique_error_line_pairs.csv`** — One row per unique (exception, line) pair that triggered a bug/crash/timeout, with a representative input.
- **`bug_counts.csv`** — Merged from per-worker `logs/bug_counts.csv` under `.worker_cwd/` when present, otherwise copied from the target’s canonical `logs/` (same flow for json-decoder and binary targets). Json-decoder counts are updated during each fuzz iteration (no STV rerun at export).

### Batch runs (configs)

When running with `--config` or `--configs-dir`, a **batch folder** is created under `results/`:

```
results/batch_<timestamp>/
```

Inside it, each config gets its own folder and each repeated run gets a subfolder:

```
results/batch_<timestamp>/<config_label>/run_<n>_<timestamp>/
```

Each run subfolder contains the same artifacts as a single run (including `config.json`).

After the full run plan finishes, an overview report is written into the batch folder:

- **`report.html`** — charts + tables comparing configs
- **`report.json`** — raw aggregated metrics used by the report
