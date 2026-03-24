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


def ip_leading_zeros(ip_str: str, rng: random.Random) -> str:
    """Inject leading zeros into IPv4 octets: 1.2.3.4 -> 01.002.003.04.
    Some parsers treat these as octal, which is a classic bug source.
    """
    if "." not in ip_str:
        return ip_str
    # Only touch the host part (before any / or zone)
    base = ip_str.split("/")[0].split("%")[0]
    suffix = ip_str[len(base):]
    octets = base.split(".")
    mutated = []
    for octet in octets:
        if octet.isdigit() and rng.random() < 0.6:
            pad = rng.randint(1, 3)
            mutated.append("0" * pad + octet)
        else:
            mutated.append(octet)
    return ".".join(mutated) + suffix


def ip_embedded_ipv4(ip_str: str, rng: random.Random) -> str:
    """Generate an IPv4-mapped or IPv4-compatible IPv6 address.
    e.g. ::ffff:192.168.0.1 or 64:ff9b::10.0.0.1
    """
    ipv4_pool = [
        "127.0.0.1", "0.0.0.0", "255.255.255.255",
        "192.168.0.1", "10.0.0.1", "172.16.0.1",
    ]
    ipv4 = rng.choice(ipv4_pool)
    prefix = rng.choice([
        "::ffff:",
        "::ffff:0:",
        "64:ff9b::",
        "2002:",          # 6to4 tunnel address start
        "::0:",
    ])
    return prefix + ipv4


def ip_zone_id(ip_str: str, rng: random.Random) -> str:
    """Append a zone ID to an IPv6 address: fe80::1 -> fe80::1%eth0.
    Zone IDs are only valid on link-local addresses but some parsers accept
    them anywhere, and the % encoding is a common confusion point.
    """
    if ":" not in ip_str:
        return ip_str
    # Strip existing zone/prefix
    base = ip_str.split("%")[0].split("/")[0]
    zones = ["%eth0", "%lo", "%en0", "%25eth0", "%en1", "%0", "%"]
    return base + rng.choice(zones)


def ip_mixed_case_hex(ip_str: str, rng: random.Random) -> str:
    """Randomise the case of hex characters in IPv6 hextets.
    e.g. fe80::AbCd or FE80::FFFF
    """
    if ":" not in ip_str:
        return ip_str
    result = []
    for ch in ip_str:
        if ch in "abcdefABCDEF" and rng.random() < 0.5:
            result.append(ch.upper() if ch.islower() else ch.lower())
        else:
            result.append(ch)
    return "".join(result)


def ip_truncate(ip_str: str, rng: random.Random) -> str:
    """Drop or duplicate an octet/hextet to produce structurally malformed addresses.
    e.g. 1.2.3.4 -> 1.2.3  or  1.2.3.4 -> 1.2.3.4.4
    """
    if "." in ip_str:
        base = ip_str.split("/")[0].split("%")[0]
        suffix = ip_str[len(base):]
        parts = base.split(".")
        if not parts:
            return ip_str
        action = rng.choice(["drop", "duplicate", "swap"])
        idx = rng.randrange(len(parts))
        if action == "drop" and len(parts) > 1:
            parts.pop(idx)
        elif action == "duplicate":
            parts.insert(idx, parts[idx])
        elif action == "swap" and len(parts) > 1:
            other = rng.randrange(len(parts))
            parts[idx], parts[other] = parts[other], parts[idx]
        return ".".join(parts) + suffix

    if ":" in ip_str:
        base = ip_str.split("%")[0].split("/")[0]
        suffix = ip_str[len(base):]
        # Split on :: carefully
        if "::" in base:
            left, _, right = base.partition("::")
            side = rng.choice(["left", "right"])
            parts = (left if side == "left" else right).split(":") if (left if side == "left" else right) else []
            if parts:
                idx = rng.randrange(len(parts))
                parts.pop(idx)
            rejoined = (":".join(parts) if parts else "")
            base = (left if side == "right" else rejoined) + "::" + (right if side == "left" else rejoined)
        else:
            parts = base.split(":")
            if len(parts) > 1:
                idx = rng.randrange(len(parts))
                parts.pop(idx)
            base = ":".join(parts)
        return base + suffix

    return ip_str


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
            # Aggressively reward finding new coverage (5x weight)
            # This helps rare successes (Grammar) break through the noise
            self.weights[operator] = self.weights[operator] * 4.0
        else:
            # Slightly decrease weight to avoid stagnation
            self.weights[operator] = max(0.1, self.weights[operator] * (1 - self.alpha * 0.1))

    def get_probabilities(self) -> dict[str, float]:
        total = sum(self.weights.values())
        return {op: w / total for op, w in self.weights.items()}

    def get_group_stats(self, op_to_group: dict[str, str]) -> dict[str, dict[str, float]]:
        # 1. Initialize result dictionary
        stats: dict[str, dict[str, float]] = {}
        
        # 2. Iterate through the mapping we passed in (e.g. "bit_flip" -> "Byte Havoc")
        for op, group in op_to_group.items():
            if op not in self.weights: continue # Safety check
            
            # 3. Sum up the weights, usage count, and success count for all ops in this group
            g = stats.setdefault(group, {"weight": 0.0, "usage": 0.0, "success": 0.0})
            g["weight"] += self.weights[op]
            g["usage"] += self.usage[op]
            g["success"] += self.success[op]

        # 4. Calculate final percentages
        total_weight = sum(s["weight"] for s in stats.values())
        for s in stats.values():
            # Probability: The % of time the fuzzer picks this category
            s["probability"] = s["weight"] / total_weight if total_weight > 0 else 0.0
            # Success Rate: How often this category actually found code coverage
            s["success_rate"] = s["success"] / s["usage"] if s["usage"] > 0 else 0.0
        return stats

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
    "leading_zeros": ip_leading_zeros,
    "embedded_ipv4": ip_embedded_ipv4,
    "zone_id": ip_zone_id,
    "mixed_case_hex": ip_mixed_case_hex,
    "truncate": ip_truncate,
}
