# Seed Corpus (AFL-style Usage)

Use this package to load reproducible seeds and sample by `target_or_family`.

## Run the demo

From repo root:

```bash
python3 -m seed_corpus.demo
```

Or from `seed_corpus/`:

```bash
python3 demo.py
```

## Core API

```python
import random
from seed_corpus import SeedCorpus

corpus = SeedCorpus.load()
```

### 1) Single-seed sampling (deterministic with RNG)

```python
rng = random.Random(1337)
seed = corpus.sample("json-decoder", rng=rng)

data = seed.text      # primary value (string input)
# data_bytes = seed.to_bytes()  # only if you need bytes
print(seed.seed_id, seed.bucket, seed.label)
```

### 2) Batch sampling by ratio (main API for scheduler experiments)

```python
rng = random.Random(42)
batch = corpus.sample_ratio_batch(
    "cidrize-runner",  # also: json-decoder, ipv4-parser, ipv6-parser
    total=50,
    bucket_ratios={"valid": 0.7, "string_stress": 0.2, "near_valid": 0.1},
    rng=rng,
    shuffle=True,
)
```

Behavior:
- exact `total`
- exact bucket counts from ratios (largest-remainder rounding)
- no replacement (overflow raises `ValueError`)
- `cidrize-runner` pools `ipv4` + `ipv6` and splits evenly by family (e.g. `50 -> 25/25`)

## Targets / families

- `json-decoder` -> `json`
- `ipv4-parser` -> `ipv4`
- `ipv6-parser` -> `ipv6`
- `cidrize-runner` -> grouped target (`ipv4` + `ipv6`)

## AFL loop integration (minimal)

```python
for seed in corpus.sample_ratio_batch("json-decoder", 40, {"valid": 0.5, "string_stress": 0.25, "near_valid": 0.25}, rng=random.Random(1)):
    input_text = seed.text
    # mutate -> run target -> collect signals -> compute isinteresting_score
    # log seed.seed_id, seed.bucket, isinteresting_score
```
