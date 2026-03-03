from __future__ import annotations

from pathlib import Path


FUZZER_ROOT = Path(__file__).resolve().parent
RESULTS_DIR = FUZZER_ROOT / "results"
DISCOVERED_SEED_ORDINAL_BASE = 1_000_000
JSON_DECODER_TARGET_DIR = FUZZER_ROOT / "targets" / "json-decoder"
JSON_DECODER_STV_SCRIPT = JSON_DECODER_TARGET_DIR / "json_decoder_stv.py"

