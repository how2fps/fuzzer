from __future__ import annotations

from pathlib import Path


# Project root (one level above `core/`)
FUZZER_ROOT = Path(__file__).resolve().parent.parent

# Where all fuzzing result directories will be created, e.g.:
# <project_root>/results/json-decoder_20260304_.... 
RESULTS_DIR = FUZZER_ROOT / "results"

DISCOVERED_SEED_ORDINAL_BASE = 1_000_000

# Location of the json-decoder target implementation and STV script
JSON_DECODER_TARGET_DIR = FUZZER_ROOT / "targets" / "json-decoder"
JSON_DECODER_STV_SCRIPT = JSON_DECODER_TARGET_DIR / "json_decoder_stv.py"

