from __future__ import annotations

import json
import random
from typing import Any


def json_mutate_keys(data: Any, rng: random.Random) -> Any:
    if isinstance(data, dict) and data:
        key = rng.choice(list(data.keys()))
        new_key = key + rng.choice(["_idx", "!", "\x00", "A" * 10])
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
        if isinstance(data, str):
            return data[::-1]
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
            data[idx] = data[idx][0]
    return data


def json_duplicate_keys(data: Any, rng: random.Random) -> Any:
    if isinstance(data, dict) and data:
        key = rng.choice(list(data.keys()))
        raw = json.dumps(data)
        pattern = f'"{key}":'
        replacement = f'"{key}": {json.dumps(data[key])}, "{key}":'
        return raw.replace(pattern, replacement, 1)
    return json.dumps(data)


def json_numeric_edge_case(data: Any, rng: random.Random) -> Any:
    edge_cases = [
        0,
        -1,
        2**31 - 1,
        -(2**31),
        2**63 - 1,
        -(2**63),
        float("nan"),
        float("inf"),
        -float("inf"),
    ]
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


def _split_ip_base_suffix(ip_str: str) -> tuple[str, str]:
    base = ip_str
    suffix = ""
    if "/" in base:
        base, prefix = base.split("/", 1)
        suffix = f"/{prefix}"
    if "%" in base:
        base, zone = base.split("%", 1)
        suffix = f"%{zone}{suffix}"
    return base, suffix


def _join_ip_base_suffix(base: str, suffix: str) -> str:
    return base + suffix


def ip_mutate_octet_hextet(ip_str: str, rng: random.Random) -> str:
    base, suffix = _split_ip_base_suffix(ip_str)
    if ":" in base:
        parts = base.split(":")
        non_empty = [index for index, part in enumerate(parts) if part]
        if non_empty:
            parts[rng.choice(non_empty)] = hex(rng.randint(0, 0x1FFFF))[2:]
            return _join_ip_base_suffix(":".join(parts), suffix)
        return _join_ip_base_suffix(base + "gggg", suffix)

    if "." in base:
        octets = base.split(".")
        if octets:
            octets[rng.randrange(len(octets))] = str(rng.randint(0, 512))
            return _join_ip_base_suffix(".".join(octets), suffix)
    return ip_str


def ip_mutate_prefix_length(ip_str: str, rng: random.Random) -> str:
    if "/" in ip_str:
        base, _prefix = ip_str.split("/", 1)
        max_prefix = 128 if ":" in base else 32
        candidate_prefix = rng.choice((0, max_prefix, max_prefix + 1, -1, rng.randint(0, max_prefix + 16)))
        return f"{base}/{candidate_prefix}"
    max_prefix = 128 if ":" in ip_str else 32
    return f"{ip_str}/{rng.randint(0, max_prefix + 16)}"


def ip_compression_variant(ip_str: str, rng: random.Random) -> str:
    if ":" not in ip_str:
        return ip_str
    if "::" in ip_str:
        return ip_str.replace("::", ":0:0:0:")
    parts = ip_str.split(":")
    if len(parts) > 2:
        idx = rng.randrange(len(parts) - 1)
        return ":".join(parts[:idx]) + "::" + ":".join(parts[idx + 2 :])
    return ip_str


def ip_separator_whitespace(ip_str: str, rng: random.Random) -> str:
    seps = [" ", "\t", "-", "_", ".\t.", ":: "]
    idx = rng.randrange(len(ip_str)) if ip_str else 0
    return ip_str[:idx] + rng.choice(seps) + ip_str[idx:]


def ip_near_valid_malformed(ip_str: str, rng: random.Random) -> str:
    base, suffix = _split_ip_base_suffix(ip_str)
    if ":" in base:
        if rng.random() < 0.5:
            return _join_ip_base_suffix(base + ":ffff:ffff", suffix)
        return _join_ip_base_suffix(base.replace("::", ":::", 1), suffix)
    if "." in base:
        if rng.random() > 0.5:
            return _join_ip_base_suffix(base + ".1", suffix)
        return _join_ip_base_suffix(base.replace("255", "256"), suffix)
    return ip_str + "/999"


def ip_leading_zeros(ip_str: str, rng: random.Random) -> str:
    if "." not in ip_str:
        return ip_str
    base = ip_str.split("/")[0].split("%")[0]
    suffix = ip_str[len(base) :]
    octets = base.split(".")
    mutated: list[str] = []
    for octet in octets:
        if octet.isdigit() and rng.random() < 0.6:
            mutated.append(("0" * rng.randint(1, 3)) + octet)
        else:
            mutated.append(octet)
    return ".".join(mutated) + suffix


def ip_embedded_ipv4(_ip_str: str, rng: random.Random) -> str:
    ipv4_pool = [
        "127.0.0.1",
        "0.0.0.0",
        "255.255.255.255",
        "192.168.0.1",
        "10.0.0.1",
        "172.16.0.1",
    ]
    prefix = rng.choice(["::ffff:", "::ffff:0:", "64:ff9b::", "2002:", "::0:"])
    return prefix + rng.choice(ipv4_pool)


def ip_zone_id(ip_str: str, rng: random.Random) -> str:
    if ":" not in ip_str:
        return ip_str
    base = ip_str.split("%")[0].split("/")[0]
    return base + rng.choice(["%eth0", "%lo", "%en0", "%25eth0", "%en1", "%0", "%"])


def ip_mixed_case_hex(ip_str: str, rng: random.Random) -> str:
    if ":" not in ip_str:
        return ip_str
    result: list[str] = []
    for ch in ip_str:
        if ch in "abcdefABCDEF" and rng.random() < 0.5:
            result.append(ch.upper() if ch.islower() else ch.lower())
        else:
            result.append(ch)
    return "".join(result)


def ip_truncate(ip_str: str, rng: random.Random) -> str:
    if "." in ip_str:
        base = ip_str.split("/")[0].split("%")[0]
        suffix = ip_str[len(base) :]
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
        suffix = ip_str[len(base) :]
        if "::" in base:
            left, _, right = base.partition("::")
            side = rng.choice(["left", "right"])
            raw_parts = left if side == "left" else right
            parts = raw_parts.split(":") if raw_parts else []
            if parts:
                parts.pop(rng.randrange(len(parts)))
            rejoined = ":".join(parts) if parts else ""
            base = (left if side == "right" else rejoined) + "::" + (
                right if side == "left" else rejoined
            )
        else:
            parts = base.split(":")
            if len(parts) > 1:
                parts.pop(rng.randrange(len(parts)))
            base = ":".join(parts)
        return base + suffix

    return ip_str


def ip_ipv4_boundary_pressure(ip_str: str, rng: random.Random) -> str:
    pool = [
        "0.0.0.0",
        "255.255.255.255",
        "127.0.0.1",
        "192.168.0.1",
        "1.1.1.1",
        "256.0.0.1",
        "999.999.999.999",
        "192.168.1",
        "01.002.003.004",
    ]
    candidate = rng.choice(pool)
    if "/" in ip_str:
        candidate = f"{candidate}/{rng.choice((0, 24, 32, 33, 999))}"
    return candidate


def ip_ipv6_boundary_pressure(ip_str: str, rng: random.Random) -> str:
    pool = [
        "::",
        "::1",
        "2001:db8::1",
        "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
        "fe80::1%eth0",
        "::ffff:192.168.0.1",
        "2001:db8:::1",
        "gggg::1",
        "1:2:3:4:5:6:7:8:9",
        "::ffff:999.999.1.1",
    ]
    candidate = rng.choice(pool)
    if "/" in ip_str:
        candidate = f"{candidate}/{rng.choice((0, 64, 128, 129, 999))}"
    return candidate


def ip_prefix_zone_shuffle(ip_str: str, rng: random.Random) -> str:
    if ":" not in ip_str:
        return ip_str + rng.choice(("/33", "/999", "%eth0"))

    if "/" in ip_str and "%" in ip_str:
        base, prefix = ip_str.split("/", 1)
        addr, zone = base.split("%", 1)
        return f"{addr}/{prefix}%{zone}"
    if "/" in ip_str:
        base, prefix = ip_str.split("/", 1)
        return f"{base}%eth0/{prefix}"
    if "%" in ip_str:
        base, zone = ip_str.split("%", 1)
        return f"{base}%{zone}/129"
    return ip_str + "%eth0/129"


def ip_delimiter_overload(ip_str: str, rng: random.Random) -> str:
    if ":" in ip_str:
        return ip_str.replace(":", rng.choice((":::", "::::", " : ")), 1)
    if "." in ip_str:
        return ip_str.replace(".", rng.choice(("..", "...", ". .")), 1)
    return ip_str + ":::"


def ip_unbalanced_brackets(ip_str: str, rng: random.Random) -> str:
    if ":" in ip_str:
        return rng.choice(
            (
                f"[{ip_str}",
                f"{ip_str}]",
                f"[{ip_str}]:{rng.randint(0, 99999)}",
            )
        )
    return f"[{ip_str}]"


class AdaptiveStrategy:
    def __init__(self, operators: list[str]) -> None:
        self.weights = {op: 1.0 for op in operators}
        self.usage = {op: 0 for op in operators}
        self.success = {op: 0 for op in operators}
        self.velocities = {op: 0.0 for op in operators}
        self.local_best_efficiencies = {op: 0.0 for op in operators}
        self.global_best_efficiency = 0.0
        self.cycle_usage = {op: 0 for op in operators}
        self.cycle_success = {op: 0 for op in operators}
        self.cycle_count = 0
        self.cycle_window = 50
        self.w = 0.4
        self.epsilon = 0.05

    def select_operator(self, rng: random.Random) -> str:
        ops = list(self.weights.keys())
        if rng.random() < self.epsilon:
            op = rng.choice(ops)
        else:
            op = rng.choices(ops, weights=list(self.weights.values()), k=1)[0]
        self.usage[op] += 1
        self.cycle_usage[op] += 1
        return op

    def update_score(self, operator: str, gained_new_coverage: bool) -> None:
        if gained_new_coverage:
            self.success[operator] += 1
            self.cycle_success[operator] += 1
        self.cycle_count += 1
        if self.cycle_count >= self.cycle_window:
            self.pso_update()
            self.cycle_count = 0
            for op in self.cycle_usage:
                self.cycle_usage[op] = 0
                self.cycle_success[op] = 0

    def pso_update(self) -> None:
        for op in self.weights:
            eff = (
                self.cycle_success[op] / self.cycle_usage[op]
                if self.cycle_usage[op] > 0
                else 0.0
            )
            if eff > self.local_best_efficiencies[op]:
                self.local_best_efficiencies[op] = eff
            if eff > self.global_best_efficiency:
                self.global_best_efficiency = eff

        for op in self.weights:
            r1 = random.random()
            r2 = random.random()
            x_now = self.weights[op]
            v_now = self.velocities[op]
            l_target = self.local_best_efficiencies[op] * 500.0
            g_target = self.global_best_efficiency * 500.0
            new_v = (self.w * v_now) + (r1 * (l_target - x_now)) + (
                r2 * (g_target - x_now)
            )
            self.velocities[op] = new_v
            self.weights[op] = max(0.1, x_now + new_v)


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
    "ipv4_boundary_pressure": ip_ipv4_boundary_pressure,
    "ipv6_boundary_pressure": ip_ipv6_boundary_pressure,
    "prefix_zone_shuffle": ip_prefix_zone_shuffle,
    "delimiter_overload": ip_delimiter_overload,
    "unbalanced_brackets": ip_unbalanced_brackets,
}
