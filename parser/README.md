## Parser module

This module runs fuzzer inputs against a selected target and emits a normalized JSON result. It is designed to be called as a library (`run_parser`) or as a CLI (`parser.py`).

### Available targets

The hard‑coded targets are defined in `parser.py` in the `TARGETS` mapping:

- **cidrize-runner**: closed binary that calls `cidrize`; has open target `cidrize`.
- **IPv4-IPv6-parser**: closed binary IPv4/IPv6 parser; has open target `ipyparse`.
- **cidrize**: open Python target (invoked via `uv run cidr`).
- **ipyparse**: open Python library target (reads input from stdin).
- **json-decoder**: buggy JSON decoder with coverage and bug categorization.
- **json_open**: open equivalent for `json-decoder` that uses Python’s stdlib `json` with the same bug-signature format.

### Library usage

You normally interact with the parser through `run_parser`:

```python
from parser.parser import run_parser

result = run_parser(
    input_data=b'{"a": 1}',
    target="json-decoder",      # or "cidrize-runner", "json_open", etc.
    timeout=5.0,
    print_json=False,
)
```

Arguments:

- **input_data**: bytes payload to feed to the target (mutated input from the fuzzer).
- **input_path**: optional path to a file containing the input (mutually exclusive with `input_data`).
- **target**: target name (key in `TARGETS`).
- **timeout**: per‑run timeout in seconds (default `10.0`).
- **print_json**: if `True`, pretty‑prints the result JSON to stdout.

Exactly one of `input_data` or `input_path` must be provided; if neither is given, `run_parser` reads from stdin.

### CLI usage

From the project root:

```bash
uv run parser/parser.py --target json-decoder --input-path path/to/input.json
```

or, using stdin:

```bash
echo '{"a":1}' | uv run parser/parser.py --target json-decoder
```

(If you are using the project’s virtualenv directly, replace `uv run` with `python` and ensure `PYTHONPATH` includes the project root.)

### Result JSON schema (top level)

For any target, `run_parser` returns a dict of the form:

- **target**: target name.
- **status**: `"ok"`, `"bug"`, `"crash"`, or `"timeout"`.
- **stdout_signature**: normalized hash of stdout.
- **stderr_signature**: normalized hash of stderr.
- **bug_signature**: object with:
  - `type`: bug category or `None`.
  - `exception`: exception class name or `None`.
  - `message`: error message or `None`.
  - `file`: file path associated with the bug, if any.
  - `line`: line number as string, if any.
- **semantic_output**: normalized semantic representation of the output (or `None`).
- **coverage_bitmap**: list of integers for coverage (only populated for coverage‑enabled targets, e.g. `json_open`), else `None`.

For closed targets with an open equivalent (`cidrize-runner`, `IPv4-IPv6-parser`), the result also includes:

- **open_result**: another result dict in the same format, produced by running the open target against the same input.

For the `json-decoder` target, an additional field is included:

- **json_decoder_details**: dict returned by `run_json_decoder_with_branches`, containing:
  - `status`: `"ok"` or `"bug"`.
  - `bug_signature`: same shape as top‑level `bug_signature`.
  - `decoded`: JSON‑serializable representation of the decoded value (or `repr` fallback).
  - `coverage_file`: coverage data file path.
  - `covered_branches`: integer.
  - `missing_branches`: integer.
  - `branch_details_by_file`: per‑file coverage details.

For the `json_open` target, the underlying helper `json_open_runner.py` returns compatible fields:

- `status`, `bug_signature`, and `decoded` are embedded into the main parser result via `semantic_output` and `bug_signature`.

