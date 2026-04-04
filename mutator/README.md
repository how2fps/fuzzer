## Mutator module

This package contains grammar‑aware generators and byte‑level mutators that you can plug into any fuzzing loop. It is independent of the parser/targets layer.

### Installation / import

- **As a package inside this repo**:

```python
from mutator.versions.lib import (
    generate_from_grammar,
    mutate_text_with_grammar,
    resolve_grammar_spec,
    bit_flip,
    arithmetic_mutation,
    interesting_value_mutation,
    delete_block_mutation,
    clone_block_mutation,
)
```

Everything is plain functions with type hints; there is no class state to manage.

### Grammar-driven fuzzing

- **Generate fresh input from the active grammar**:

```python
grammar_spec = resolve_grammar_spec(kind="grammar")
seed = generate_from_grammar(grammar_spec=grammar_spec)
data = seed.encode("utf-8")
```

- **Mutate an existing seed while staying grammar-aware**:

```python
next_seed = mutate_text_with_grammar(
    original_text=seed,
    grammar_spec=grammar_spec,
)
data = next_seed.encode("utf-8")
```

`max_depth` controls how deeply recursive generated structures get, and `regenerate_probability` controls how often a completely new sample is generated instead of editing the old one.

### Byte‑level mutation primitives

These helpers work on raw `bytes`/`bytearray` and are format‑agnostic, so you can layer them on top of grammar-generated inputs or use them directly for binary fuzzing.

- **Bit flip in a random byte**:

```python
mutated = bit_flip(data=payload)
```

- **Small arithmetic tweak on one byte**:

```python
mutated = arithmetic_mutation(data=payload)
```

- **Replace one byte with an “interesting” value (0x01, 0xFF, etc.)**:

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
from mutator.versions.lib import (
    bit_flip,
    generate_from_grammar,
    mutate_text_with_grammar,
    resolve_grammar_spec,
)

def fuzz_one_iteration(previous_seed: str | None) -> bytes:
    grammar_spec = resolve_grammar_spec(kind="grammar")
    if previous_seed is None:
        seed = generate_from_grammar(grammar_spec=grammar_spec)
    else:
        seed = mutate_text_with_grammar(
            original_text=previous_seed,
            grammar_spec=grammar_spec,
        )
    payload = seed.encode("utf-8")
    if random.random() < 0.5:
        payload = bit_flip(data=payload)
    return payload
```

### Mutator versions

- `base`: grammar-driven text mutation
- `byte_havoc`: AFL-style byte-level mutations
- `grammar_ast`: generalized grammar-AST mutator inspired by `mutator_test.py`; mutates generic grammar node classes (`Sequence`, `Alternation`, `Repeat`, `Literal`, `Ref`, etc.), supports extra DSL rules via `-g/--grammar-rules-file`, and can now parse a seed into an exact derivation tree under an explicit grammar start rule via `parse_from_rule(...)` / `mutate_from_rule(...)`

### `grammar_ast` exact seed parsing

For built-in `json` and `ip` fuzzing, `grammar_ast.mutate(...)` first tries to parse
the seed into a mutable seed tree, mutate that tree, and then serialize it back to
text.

For external grammar files, you can also use the generic exact parser directly when
you know the start rule:

```python
from mutator.versions import grammar_ast
import random

grammar_ast.configure(grammar_rules_file="examples/url_rules.txt")
seed = "https://openai.com/docs?id=42"

tree = grammar_ast.parse_from_rule(text=seed, start_rule="url_start")
mutated = grammar_ast.mutate_from_rule(
    seed,
    start_rule="url_start",
    rng=random.Random(7),
    blend_with_seed=False,
)
```

That path is what makes new grammar files extensible without adding new
format-specific mutator code first: once a seed can be parsed under a rule, the
same generic tree mutations can add, delete, duplicate, swap, or replace nodes in
the matched derivation tree.
