from __future__ import annotations

from pathlib import Path


# Project root (one level above `core/`)
FUZZER_ROOT = Path(__file__).resolve().parent.parent

# Where all fuzzing result directories will be created, e.g.:
# <project_root>/results/json-decoder_20260304_.... 
RESULTS_DIR = FUZZER_ROOT / "results"

# Where target implementations live (project root / targets)
TARGETS_DIR = FUZZER_ROOT / "targets"

# Config files for batch/single runs (copy configs/_template.json to add new runs)
CONFIGS_DIR = FUZZER_ROOT / "configs"

DISCOVERED_SEED_ORDINAL_BASE = 1_000_000

# Location of the json-decoder target implementation
JSON_DECODER_TARGET_DIR = FUZZER_ROOT / "targets" / "json-decoder"

