## Mutator module

This package contains grammar‑aware generators and byte‑level mutators that you can plug into any fuzzing loop. It is independent of the parser/targets layer.

### Installation / import

- **As a package inside this repo**:

```python
from mutator.mutator import (
    generate_json_input,
    generate_ip_input,
    generate_ipv4_input,
    generate_ipv6_input,
    mutate_json_input,
    mutate_ip_input,
    mutate_ipv4_input,
    mutate_ipv6_input,
    bit_flip,
    arithmetic_mutation,
    interesting_value_mutation,
    delete_block_mutation,
    clone_block_mutation,
)
```

Everything is plain functions with type hints; there is no class state to manage.

### JSON fuzzing

- **Generate fresh JSON from the grammar**:

```python
seed = generate_json_input()
data = seed.encode("utf-8")
```

- **Mutate an existing JSON string, staying grammar‑shaped**:

```python
next_seed = mutate_json_input(original_text=seed)
data = next_seed.encode("utf-8")
```

`max_depth` controls how deeply recursive the JSON structures get, and `regenerate_probability` controls how often a completely new sample is generated instead of editing the old one.

### IP fuzzing (IPv4 + IPv6)

You can either fuzz both families together or focus on one.

- **Any IP (v4 or v6, with optional prefix)**:

```python
ip_seed = generate_ip_input()          # may be IPv4 or IPv6
ip_next = mutate_ip_input(original_text=ip_seed)
```

- **IPv4‑only**:

```python
ipv4_seed = generate_ipv4_input()
ipv4_next = mutate_ipv4_input(original_text=ipv4_seed)
```

- **IPv6‑only**:

```python
ipv6_seed = generate_ipv6_input()
ipv6_next = mutate_ipv6_input(original_text=ipv6_seed)
```

All IP helpers produce strings; encode to bytes before sending to a target:

```python
payload = ipv4_next.encode("utf-8")
```

### Byte‑level mutation primitives

These helpers work on raw `bytes`/`bytearray` and are format‑agnostic, so you can layer them on top of JSON/IP generators or use them directly for binary fuzzing.

- **Bit flip in a random byte**:

```python
mutated = bit_flip(data=payload)
```

- **Small arithmetic tweak on one byte**:

```python
mutated = arithmetic_mutation(data=payload)
```

- **Replace one byte with an “interesting” value (0x00, 0xFF, etc.)**:

```python
mutated = interesting_value_mutation(data=payload)
```

- **Delete a random contiguous block**:

```python
mutated = delete_block_mutation(data=payload)
```

- **Clone and insert a random block somewhere else**:

```python
mutated = clone_block_mutation(data=payload)
```

All mutators are pure functions: they return new `bytes` and never modify the original `data` object in place (a `bytearray` is copied before edits).

### Example fuzzing loop sketch

```python
import random
from mutator.mutator import generate_json_input, mutate_json_input, bit_flip

def fuzz_one_iteration(previous_seed: str | None) -> bytes:
    if previous_seed is None:
        seed = generate_json_input()
    else:
        seed = mutate_json_input(original_text=previous_seed)
    payload = seed.encode("utf-8")
    if random.random() < 0.5:
        payload = bit_flip(data=payload)
    return payload
```

You can adapt the same pattern for `generate_ip_input`/`mutate_ip_input` (or the v4/v6‑specific variants) depending on which targets you are fuzzing.

