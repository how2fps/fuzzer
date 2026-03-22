import random
import json
import re
from typing import Any, Callable

# --- JSON Operators ---

def json_mutate_keys(data: Any, rng: random.Random) -> Any:
    if isinstance(data, dict) and data:
        key = rng.choice(list(data.keys()))
        # Mutate the key (e.g., bit flip or append junk)
        new_key = key + rng.choice(["_idx", "!", "\x00", "A"*10])
        data[new_key] = data.pop(key)
    elif isinstance(data, list) and data:
        idx = rng.randrange(len(data))
        data[idx] = json_mutate_keys(data[idx], rng)
    return data

def json_mutate_values(data: Any, rng: random.Random) -> Any:
    if isinstance(data, dict) and data:
        key = rng.choice(list(data.keys()))
        data[key] = json_mutate_values(data[key], rng)
    elif isinstance(data, list) and data:
        idx = rng.randrange(len(data))
        data[idx] = json_mutate_values(data[idx], rng)
    else:
        # It's a terminal value
        if isinstance(data, str):
            return data[::-1] # simple flip
        if isinstance(data, (int, float)):
            return data + rng.randint(-100, 100)
    return data

def json_deepen_nesting(data: Any, rng: random.Random) -> Any:
    if isinstance(data, dict) and data:
        key = rng.choice(list(data.keys()))
        data[key] = {"nested_key": data[key]}
    elif isinstance(data, list) and data:
        idx = rng.randrange(len(data))
        data[idx] = [data[idx]]
    return data

def json_flatten_nesting(data: Any, rng: random.Random) -> Any:
    if isinstance(data, dict) and data:
        key = rng.choice(list(data.keys()))
        if isinstance(data[key], dict) and data[key]:
            inner_key = rng.choice(list(data[key].keys()))
            data[inner_key] = data[key][inner_key]
            del data[key]
    elif isinstance(data, list) and data:
        idx = rng.randrange(len(data))
        if isinstance(data[idx], list) and data[idx]:
            val = data[idx][0]
            data[idx] = val
    return data

def json_duplicate_keys(data: Any, rng: random.Random) -> Any:
    # Note: dicts can't have duplicate keys in Python, so we produce raw string for this
    if isinstance(data, dict) and data:
        key = rng.choice(list(data.keys()))
        raw = json.dumps(data)
        # Manually inject duplicate key
        pattern = f'"{key}":'
        replacement = f'"{key}": {json.dumps(data[key])}, "{key}":'
        return raw.replace(pattern, replacement, 1)
    return json.dumps(data)

def json_numeric_edge_case(data: Any, rng: random.Random) -> Any:
    edge_cases = [0, -1, 2**31-1, -2**31, 2**63-1, -2**63, float('nan'), float('inf'), -float('inf')]
    if isinstance(data, (int, float)):
        return rng.choice(edge_cases)
    if isinstance(data, dict) and data:
        key = rng.choice(list(data.keys()))
        data[key] = json_numeric_edge_case(data[key], rng)
    elif isinstance(data, list) and data:
        idx = rng.randrange(len(data))
        data[idx] = json_numeric_edge_case(data[idx], rng)
    return data

def json_escape_unicode(data: Any, rng: random.Random) -> Any:
    escapes = ["\\u0000", "\\uFFFF", "\\ud83d\\ude00", "\\n", "\\r\\n", "\t", "\x00"]
    if isinstance(data, str):
        return data + rng.choice(escapes)
    if isinstance(data, dict) and data:
        key = rng.choice(list(data.keys()))
        data[key] = json_escape_unicode(data[key], rng)
    return data


# --- IP/CIDR Operators ---

def ip_mutate_octet_hextet(ip_str: str, rng: random.Random) -> str:
    if "." in ip_str: # IPv4
        octets = ip_str.split(".")
        if len(octets) >= 1:
            idx = rng.randrange(len(octets))
            octets[idx] = str(rng.randint(0, 512)) # intentionally malformed
            return ".".join(octets)
    elif ":" in ip_str: # IPv6
        parts = ip_str.split(":")
        if len(parts) >= 1:
            idx = rng.randrange(len(parts))
            parts[idx] = hex(rng.randint(0, 0x1FFFF))[2:]
            return ":".join(parts)
    return ip_str

def ip_mutate_prefix_length(ip_str: str, rng: random.Random) -> str:
    if "/" in ip_str:
        base, prefix = ip_str.split("/")
        new_prefix = str(rng.randint(-1, 129))
        return f"{base}/{new_prefix}"
    return ip_str + "/" + str(rng.randint(0, 128))

def ip_compression_variant(ip_str: str, rng: random.Random) -> str:
    if ":" in ip_str:
        # IPv6 compression/expansion
        if "::" in ip_str:
            # Expand (roughly)
            return ip_str.replace("::", ":0:0:0:")
        else:
            # Compress (roughly)
            parts = ip_str.split(":")
            if len(parts) > 2:
                idx = rng.randrange(len(parts) - 1)
                return ":".join(parts[:idx]) + "::" + ":".join(parts[idx+2:])
    return ip_str

def ip_separator_whitespace(ip_str: str, rng: random.Random) -> str:
    seps = [" ", "\t", "-", "_", ".\t.", ":: "]
    idx = rng.randrange(len(ip_str)) if ip_str else 0
    return ip_str[:idx] + rng.choice(seps) + ip_str[idx:]

def ip_near_valid_malformed(ip_str: str, rng: random.Random) -> str:
    # 192.168.0.256, 1.2.3.4.5, etc.
    if "." in ip_str:
        return ip_str + ".1" if rng.random() > 0.5 else ip_str.replace("255", "256")
    return ip_str + ":ffff:ffff"


# --- Adaptive Strategy (Mopt-like) ---

class AdaptiveStrategy:
    def __init__(self, operators: list[str]):
        self.weights = {op: 1.0 for op in operators}
        self.usage = {op: 0 for op in operators}
        self.success = {op: 0 for op in operators}
        self.alpha = 0.1 # Learning rate

    def select_operator(self, rng: random.Random) -> str:
        ops = list(self.weights.keys())
        weights = list(self.weights.values())
        op = rng.choices(ops, weights=weights, k=1)[0]
        self.usage[op] += 1
        return op

    def update_score(self, operator: str, gained_new_coverage: bool):
        if gained_new_coverage:
            self.success[operator] += 1
            # Increase weight
            self.weights[operator] = self.weights[operator] * (1 + self.alpha)
        else:
            # Slightly decrease weight to avoid stagnation
            self.weights[operator] = max(0.1, self.weights[operator] * (1 - self.alpha * 0.1))

    def get_probabilities(self) -> dict[str, float]:
        total = sum(self.weights.values())
        return {op: w / total for op, w in self.weights.items()}

# Mapping names to functions
JSON_OPERATORS = {
    "mutate_keys": json_mutate_keys,
    "mutate_values": json_mutate_values,
    "deepen_nesting": json_deepen_nesting,
    "flatten_nesting": json_flatten_nesting,
    "duplicate_keys": json_duplicate_keys,
    "numeric_edge_case": json_numeric_edge_case,
    "escape_unicode": json_escape_unicode,
}

IP_OPERATORS = {
    "mutate_octet_hextet": ip_mutate_octet_hextet,
    "mutate_prefix_length": ip_mutate_prefix_length,
    "compression_variant": ip_compression_variant,
    "separator_whitespace": ip_separator_whitespace,
    "near_valid_malformed": ip_near_valid_malformed,
}
