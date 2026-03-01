"""
Examples of calling run_parser with different inputs and options.

Run from project root:
    python -m parser.run_examples
    python parser/run_examples.py
Or from parser/:
    python run_examples.py
"""

import sys
from pathlib import Path

# Ensure project root is on path so "parser" is the package (e.g. when run from parser/)
_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from parser.parser import run_parser

# -----------------------------------------------------------------------------
# Example 1: Call with raw bytes (input_data)
# -----------------------------------------------------------------------------
def example_from_bytes() -> None:
    """Run parser with input passed as bytes. No JSON printed; result returned."""
    result = run_parser(
        input_data=b"192.168.1.0/24",
        target="cidrize-runner",
        timeout=5.0,
        print_json=False,
    )
    print("Example 1 (bytes, cidrize-runner): status =", result.get("status"))
    if "open_result" in result:
        print("  open_result status =", result["open_result"])


# -----------------------------------------------------------------------------
# Example 2: Call with input file path (input_path)
# -----------------------------------------------------------------------------
def example_from_file(input_file: str | Path = "sample_input.txt") -> None:
    """Run parser with input from a file. Use input_path for file-based input."""
    result = run_parser(
        input_path=input_file,
        target="IPv4-IPv6-parser",
        timeout=10.0,
        print_json=False,
    )
    print("Example 2 (file, IPv4-IPv6-parser): status =", result.get("status"))


# -----------------------------------------------------------------------------
# Example 3: Print JSON to stdout (print_json=True)
# -----------------------------------------------------------------------------
def example_print_json() -> None:
    """Run parser and print the full result as JSON to stdout."""
    run_parser(
        input_data=b'{"key": "value"}',
        target="json-decoder",
        timeout=5.0,
        print_json=True,
    )


# -----------------------------------------------------------------------------
# Example 4: Open target only (no closed/open pair)
# -----------------------------------------------------------------------------
def example_open_target_only() -> None:
    """Run an open target (e.g. cidrize or ipyparse). No open_result in response."""
    result = run_parser(
        input_data=b"10.0.0.0/8",
        target="cidrize",
        timeout=5.0,
    )
    print("Example 4 (open target cidrize): status =", result.get("status"))
    assert "open_result" not in result


# -----------------------------------------------------------------------------
# Example 5: Closed target (includes open_result)
# -----------------------------------------------------------------------------
def example_closed_target_includes_open() -> None:
    """Run a closed target; result includes open_result from the open equivalent."""
    result = run_parser(
        input_data=b"192.168.0.0/16",
        target="cidrize-runner",
        timeout=5.0,
    )
    print("Example 5 (closed cidrize-runner): status =", result.get("status"))
    if "open_result" in result:
        print("  open_result.target =", result["open_result"].get("target"))


# -----------------------------------------------------------------------------
# Example 6: Use returned result (signatures, bug_signature)
# -----------------------------------------------------------------------------
def example_inspect_result() -> None:
    """Inspect status, signatures, and bug_signature from the returned dict."""
    result = run_parser(
        input_data=b"invalid input that might crash",
        target="json-decoder",
        timeout=3.0,
    )
    print("Example 6 (inspect result):")
    print("  status:", result.get("status"))
    print("  stdout_signature:", result.get("stdout_signature"))
    print("  stderr_signature:", result.get("stderr_signature"))
    print("  bug_signature:", result.get("bug_signature"))


# -----------------------------------------------------------------------------
# Main: run examples (skip file-based if file does not exist)
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    print("--- run_parser examples ---\n")

    example_from_bytes()
    print()

    # if Path("sample_input.txt").is_file():
    #     example_from_file()
    # else:
    #     print("Example 2 skipped (sample_input.txt not found)")
    # print()

    # example_open_target_only()
    # print()

    # example_closed_target_includes_open()
    # print()

    # example_inspect_result()
    # print()

    # print("Example 3 (print full JSON for json-decoder):")
    # example_print_json()
