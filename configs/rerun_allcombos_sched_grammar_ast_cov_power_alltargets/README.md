# Experiment Matrix

This directory contains the full Cartesian product of these config variations:

- `target`: `json-decoder`, `cidrize-runner`, `IPv4-IPv6-parser`
- `seed_scheduler.scheduler_kind`: `ucb_tree`, `heap`
- `mutator.mutator_version`: `grammar_ast` (fixed for all runs)
- `parser.enable_open_coverage`: `true` (`cov-on`), `false` (`cov-off`)
- `power_scheduler.power_scheduler_version`: `constant`, `hybrid`

That gives `3 x 2 x 2 x 2 = 24` configs total, and all 24 combinations are present in this folder.

## Variation Summary

| Axis | Values tested |
|---|---|
| Target | `json-decoder`, `cidrize-runner`, `IPv4-IPv6-parser` |
| Scheduler | `ucb_tree`, `heap` |
| Mutator | `grammar_ast` |
| Open coverage | `on`, `off` |
| Power scheduler | `constant`, `hybrid` |

## Coverage Matrix

Each target was tested with the same 8 scheduler/coverage/power combinations:

| Target | Scheduler | Coverage | Power | Config |
|---|---|---|---|---|
| `json-decoder` | `ucb_tree` | `on` | `constant` | `001_json-decoder_ucb-tree_grammar-ast_cov-on_constant.json` |
| `json-decoder` | `ucb_tree` | `on` | `hybrid` | `002_json-decoder_ucb-tree_grammar-ast_cov-on_hybrid.json` |
| `json-decoder` | `ucb_tree` | `off` | `constant` | `003_json-decoder_ucb-tree_grammar-ast_cov-off_constant.json` |
| `json-decoder` | `ucb_tree` | `off` | `hybrid` | `004_json-decoder_ucb-tree_grammar-ast_cov-off_hybrid.json` |
| `json-decoder` | `heap` | `on` | `constant` | `005_json-decoder_heap_grammar-ast_cov-on_constant.json` |
| `json-decoder` | `heap` | `on` | `hybrid` | `006_json-decoder_heap_grammar-ast_cov-on_hybrid.json` |
| `json-decoder` | `heap` | `off` | `constant` | `007_json-decoder_heap_grammar-ast_cov-off_constant.json` |
| `json-decoder` | `heap` | `off` | `hybrid` | `008_json-decoder_heap_grammar-ast_cov-off_hybrid.json` |
| `cidrize-runner` | `ucb_tree` | `on` | `constant` | `009_cidrize-runner_ucb-tree_grammar-ast_cov-on_constant.json` |
| `cidrize-runner` | `ucb_tree` | `on` | `hybrid` | `010_cidrize-runner_ucb-tree_grammar-ast_cov-on_hybrid.json` |
| `cidrize-runner` | `ucb_tree` | `off` | `constant` | `011_cidrize-runner_ucb-tree_grammar-ast_cov-off_constant.json` |
| `cidrize-runner` | `ucb_tree` | `off` | `hybrid` | `012_cidrize-runner_ucb-tree_grammar-ast_cov-off_hybrid.json` |
| `cidrize-runner` | `heap` | `on` | `constant` | `013_cidrize-runner_heap_grammar-ast_cov-on_constant.json` |
| `cidrize-runner` | `heap` | `on` | `hybrid` | `014_cidrize-runner_heap_grammar-ast_cov-on_hybrid.json` |
| `cidrize-runner` | `heap` | `off` | `constant` | `015_cidrize-runner_heap_grammar-ast_cov-off_constant.json` |
| `cidrize-runner` | `heap` | `off` | `hybrid` | `016_cidrize-runner_heap_grammar-ast_cov-off_hybrid.json` |
| `IPv4-IPv6-parser` | `ucb_tree` | `on` | `constant` | `017_IPv4-IPv6-parser_ucb-tree_grammar-ast_cov-on_constant.json` |
| `IPv4-IPv6-parser` | `ucb_tree` | `on` | `hybrid` | `018_IPv4-IPv6-parser_ucb-tree_grammar-ast_cov-on_hybrid.json` |
| `IPv4-IPv6-parser` | `ucb_tree` | `off` | `constant` | `019_IPv4-IPv6-parser_ucb-tree_grammar-ast_cov-off_constant.json` |
| `IPv4-IPv6-parser` | `ucb_tree` | `off` | `hybrid` | `020_IPv4-IPv6-parser_ucb-tree_grammar-ast_cov-off_hybrid.json` |
| `IPv4-IPv6-parser` | `heap` | `on` | `constant` | `021_IPv4-IPv6-parser_heap_grammar-ast_cov-on_constant.json` |
| `IPv4-IPv6-parser` | `heap` | `on` | `hybrid` | `022_IPv4-IPv6-parser_heap_grammar-ast_cov-on_hybrid.json` |
| `IPv4-IPv6-parser` | `heap` | `off` | `constant` | `023_IPv4-IPv6-parser_heap_grammar-ast_cov-off_constant.json` |
| `IPv4-IPv6-parser` | `heap` | `off` | `hybrid` | `024_IPv4-IPv6-parser_heap_grammar-ast_cov-off_hybrid.json` |

## Per-Target Grid

`json-decoder`

| Scheduler | `cov-on + constant` | `cov-on + hybrid` | `cov-off + constant` | `cov-off + hybrid` |
|---|---|---|---|---|
| `ucb_tree` | `001` | `002` | `003` | `004` |
| `heap` | `005` | `006` | `007` | `008` |

`cidrize-runner`

| Scheduler | `cov-on + constant` | `cov-on + hybrid` | `cov-off + constant` | `cov-off + hybrid` |
|---|---|---|---|---|
| `ucb_tree` | `009` | `010` | `011` | `012` |
| `heap` | `013` | `014` | `015` | `016` |

`IPv4-IPv6-parser`

| Scheduler | `cov-on + constant` | `cov-on + hybrid` | `cov-off + constant` | `cov-off + hybrid` |
|---|---|---|---|---|
| `ucb_tree` | `017` | `018` | `019` | `020` |
| `heap` | `021` | `022` | `023` | `024` |
