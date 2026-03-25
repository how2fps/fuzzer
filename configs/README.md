# Fuzzer configs

Copy `_template.json` to a new file (e.g. `my_run.json`) and edit the values. Only include keys you want to override; others use defaults.

- **Run one config**: `python main.py --config configs/my_run.json`
- **Run one config N times**: `python main.py --config configs/my_run.json --runs 5`
- **Run all configs in this folder**: `python main.py --configs-dir configs --runs 3`

Files whose names start with `_` (like `_template.json`) are ignored when using `--configs-dir`.

The config file used for each run is saved as `config.json` in that run’s results folder (under `results/batch_*/.../run_*/config.json` when using configs).

## Config keys (all supported options)

Defaults shown below match `core.config.get_default_config()`.

- **`target`** (string, default: `json-decoder`): must be one of `parser.TARGETS` keys.
- **`scheduler_kind`** (string, default: `heap`): must be one of `seed_scheduler.list_versions()`.
- **`mutator_kind`** (string, default: `auto`): one of `auto`, `json`, `ip`.
- **`seed_preload_mode`** (string, default: `full`): one of `full`, `ratio_batch`, `sample`.
- **`seed_preload_total`** (int, default: `50`): must be `>= 0`. If set to `0`, no corpus seeds are preloaded. Combined with `seed_corpus_version: "llm_bootstrap"`, this lets the run start from LLM-generated seeds only.
- **`ucb_trace`** (bool, default: `false`): extra UCB debug logs.
- **`ucb_debug_tree`** (bool, default: `false`): prints UCB tree snapshot each iteration for `ucb_tree`.

- **`max_iterations`** (int|null, default: `10`): max iterations. **Mutually exclusive** with `max_hours`.
- **`max_hours`** (float|null, default: `null`): max time budget in hours. **Mutually exclusive** with `max_iterations`.
- **`timeout`** (float, default: `10.0`): per-input timeout in seconds.
- **`rng_seed`** (int|null, default: `null`): RNG seed for reproducibility.
- **`workers`** (int, default: `1`): number of worker processes.

- **`isinteresting_version`** (string, default: `base`): must be one of `isinteresting.list_versions()`.
- **`mutator_version`** (string, default: `base`): must be one of `mutator.list_versions()`.
- **`parser_version`** (string, default: `base`): must be one of `parser.list_versions()`.
- **`power_scheduler_version`** (string, default: `base`): must be one of `power_scheduler.list_versions()`.
- **`seed_corpus_version`** (string, default: `base`): must be one of `seed_corpus.list_versions()`.

## Minimal override example

Example `configs/my_run.json` (only override what you care about):

```json
{
  "target": "json-decoder",
  "max_iterations": 5000,
  "rng_seed": 42,
  "workers": 4
}
```
