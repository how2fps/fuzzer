# Fuzzer

AFL-style fuzzer harness that wires a seed corpus, mutator, parser, interestingness scoring, and schedulers. Each run persists results to SQLite and exports CSVs under `results/`.

## How to run

From the project root (the directory containing `main.py`):

```bash
python main.py
```

With options:

```bash
python main.py --target json-decoder --iterations 5000 --seed 42
```

### Command-line options

| Option | Default | Description |
|--------|---------|-------------|
| `--target` | `json-decoder` | Target to fuzz. Choices: `cidrize`, `cidrize-runner`, `IPv4-IPv6-parser`, `ipyparse`, `json-decoder`, `json_open` |
| `--scheduler` | `heap` | Seed scheduler. Use `python main.py` and check help for choices. |
| `--mutator` | `auto` | Mutation mode: `auto` (from target), `json`, or `ip` |
| `--iterations` | `1000` | Maximum fuzzing iterations |
| `--timeout` | `10.0` | Per-run timeout in seconds |
| `--seed` | (none) | RNG seed for reproducibility |
| `--isinteresting-version` | `base` | Interestingness module version |
| `--mutator-version` | `base` | Mutator module version |
| `--parser-version` | `base` | Parser module version |
| `--power-scheduler-version` | `base` | Power scheduler module version |
| `--seed-corpus-version` | `base` | Seed corpus module version |

### Examples

```bash
# Default: json-decoder, 1000 iterations
python main.py

# More iterations, reproducible run
python main.py --iterations 10000 --seed 12345

# Fuzz another target
python main.py --target cidrize-runner --iterations 2000

# Shorter timeout
python main.py --timeout 5.0
```

## Results

After each run, a new folder is created under `results/` named:

```
results/<target>_<timestamp>/
```

For example: `results/json-decoder_20250301_143022/`

Contents:

- **`runs.db`** — SQLite database of every iteration (seed id, seed text, mutated input, status, bug_type, exception, line, scores, etc.).
- **`runs.csv`** — Full export of `runs` as CSV.
- **`unique_error_line_pairs.csv`** — One row per unique (exception, line) pair that triggered a bug/crash/timeout, with a representative input.
- **`bug_counts.csv`** — (json-decoder only) Copy of the bug-counts CSV produced by rerunning `json_decoder_stv.py` with `--show-coverage` on the representative inputs.

Run from the repository root so that imports (`isinteresting`, `mutator`, `parser`, etc.) resolve correctly.
