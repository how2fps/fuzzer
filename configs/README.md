# Fuzzer configs

Copy `_template.json` to a new file (e.g. `my_run.json`) and edit the values. Only include keys you want to override; others use defaults.

- **Run one config**: `python main.py --config configs/my_run.json`
- **Run one config N times**: `python main.py --config configs/my_run.json --runs 5`
- **Run all configs in this folder**: `python main.py --configs-dir configs --runs 3`

Files whose names start with `_` (like `_template.json`) are ignored when using `--configs-dir`.

The config file used for each run is saved as `config.json` in that run’s results folder (under `results/batch_*/.../run_*/config.json` when using configs).

## Config shape

Config files may be written in either of these forms:

- Flat: legacy top-level keys such as `scheduler_kind`, `mutator_version`, and `seed_preload_mode`.
- Nested by module: grouped objects such as `runtime`, `seed_scheduler`, `seed_corpus`, `mutator`, `parser`, and `power_scheduler`.

The loader accepts both forms. Nested module configs are flattened internally into the runtime `FuzzConfig`.

## Module configuration table

Defaults shown below match `core.config.get_default_config()`.

| Module | Config keys | Allowed values / shape | Default |
|---|---|---|---|
| `target` | `target` | Built-in parser target name | `json-decoder` |
| `runtime` | `debug_mode` | `true` / `false` | `false` |
| `runtime` | `max_iterations` | integer or `null` | `10` |
| `runtime` | `max_hours` | float or `null` | `null` |
| `runtime` | `timeout` | float | `10.0` |
| `runtime` | `rng_seed` | integer or `null` | `null` |
| `runtime` | `workers` | integer | `1` |
| `seed_scheduler` | `scheduler_kind` | `queue`, `heap`, `ucb_tree`, `thompson` | `heap` |
| `seed_scheduler` | `ucb_trace` | `true` / `false` | `false` |
| `seed_scheduler` | `ucb_debug_tree` | `true` / `false` | `false` |
| `seed_corpus` | `seed_corpus_version` | `base`, `llm_bootstrap`, `regex-noseed` | `base` |
| `seed_corpus` | `seed_corpus_initial_draw` | `bucketed`, `random`, `full`, or `null` | `null` |
| `seed_corpus` | `seed_preload_mode` | `full`, `ratio_batch`, `sample` | `full` |
| `seed_corpus` | `seed_preload_total` | integer `>= 0` | `50` |
| `seed_corpus` | `seed_preload_bucket_ratios` | object of `{bucket_name: weight}` | project defaults |
| `seed_corpus` | `llm_seed_candidates` | integer `>= 0` | `5` |
| `mutator` | `mutator_kind` | `auto`, `json`, `ip` | `auto` |
| `mutator` | `mutator_version` | `base`, `byte_havoc`, `grammar_ast`, `adaptive_all` | `base` |
| `mutator` | `grammar_path` | file path string or `null` | `null` |
| `mutator` | `grammar_rules_file` | file path string or `null` | `null` |
| `isinteresting` | `isinteresting_version` | `base` | `base` |
| `parser` | `parser_version` | `base` | `base` |
| `parser` | `parser_config` | nested object for custom targets | `{}` |
| `parser` | `enable_open_coverage` | `true` / `false` | `false` |
| `power_scheduler` | `power_scheduler_version` | `annealing`, `base`, `constant`, `hybrid` | `base` |

## Built-in targets

These are the built-in parser targets currently available in the repo:

- `cidrize-runner`
- `IPv4-IPv6-parser`
- `cidrize`
- `ipyparse`
- `json-decoder`
- `json_open`

## Minimal override example

Example `configs/my_run.json` using the nested module form:

```json
{
  "target": "json-decoder",
  "runtime": {
    "max_iterations": 5000,
    "rng_seed": 42,
    "workers": 4
  }
}
```

## Full grouped example

```json
{
  "target": "json-decoder",
  "runtime": {
    "max_iterations": 1000,
    "workers": 4
  },
  "seed_scheduler": {
    "scheduler_kind": "ucb_tree",
    "ucb_trace": false
  },
  "seed_corpus": {
    "seed_corpus_version": "base",
    "seed_preload_mode": "ratio_batch",
    "seed_preload_total": 20
  },
  "mutator": {
    "mutator_kind": "auto",
    "mutator_version": "grammar_ast"
  },
  "isinteresting": {
    "isinteresting_version": "base"
  },
  "parser": {
    "parser_version": "base",
    "enable_open_coverage": false,
    "parser_config": {}
  },
  "power_scheduler": {
    "power_scheduler_version": "annealing"
  }
}
```

## Adding a new parser target

Use `parser_config` to define parser-only target metadata from JSON. The sample
file `configs/_template_custom_target.json` shows the full shape.

- `parser_config.targets_base_dir`: base directory that contains your target folders.
- `parser_config.targets.<name>.path`: folder for the target, relative to `targets_base_dir`.
- `parser_config.targets.<name>.command.argv`: fixed command list.
- `parser_config.targets.<name>.command.argv_template`: command list with placeholders like `{platform}`, `{exe_suffix}`, `{python_executable}`, `{project_root}`, `{parser_dir}`, `{target_dir}`, `{seed_family}`, and `{ip_version}`.
- `parser_config.targets.<name>.command.input_via_stdin`: if `true`, pass bytes on stdin.
- `parser_config.targets.<name>.command.append_input_as_final_arg`: if `false`, do not append the input as the last CLI arg when not using stdin.
- `parser_config.targets.<name>.oracle`: optional paired open/oracle target name.
- `parser_config.targets.<name>.coverage.enabled`: set `true` only for open targets already supported by `parser/open_coverage_runner.py`.
