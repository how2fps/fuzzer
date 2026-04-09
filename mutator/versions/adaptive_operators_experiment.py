from __future__ import annotations

import random
import re
import threading
from collections import deque


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
        self.cycle_window = 100  # Updated for a ~50k iteration run
        self.w = 0.4
        
        # Cooling Schedule
        self.epsilon = 0.15
        self.min_epsilon = 0.02
        self.cooling_rate = 0.99990  # Will cool down over ~20,000 iterations (almost half the session)
        
        # Pacemaker
        self.iterations_since_improvement = 0
        self.pacemaker_threshold = 3000  # Shock if no improvement in 3000 iterations (~3.6 minutes)
        self.pacemaker_shock_value = 0.50

    def select_operator(self, rng: random.Random) -> str:
        ops = list(self.weights.keys())
        if rng.random() < self.epsilon:
            op = rng.choice(ops)
        else:
            op = rng.choices(ops, weights=list(self.weights.values()), k=1)[0]
        self.usage[op] += 1
        self.cycle_usage[op] += 1
        
        # Cool down epsilon after every selection
        if self.epsilon > self.min_epsilon:
            self.epsilon = max(self.min_epsilon, self.epsilon * self.cooling_rate)
            
        return op

    def update_score(self, operator: str, gained_new_coverage: bool) -> None:
        self.iterations_since_improvement += 1
        
        if gained_new_coverage:
            self.success[operator] += 1
            self.cycle_success[operator] += 1
            self.iterations_since_improvement = 0
            
        if self.iterations_since_improvement >= self.pacemaker_threshold:
            self.epsilon = self.pacemaker_shock_value
            self.iterations_since_improvement = 0
            
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


class TokenPool:
    def __init__(self, max_size: int = 512) -> None:
        self.tokens: deque[str] = deque(maxlen=max_size)
        self.seen: set[str] = set()
        self.lock = threading.Lock()
        # Heuristics: quoted strings, words 4+, hex strings, numbers, IPs
        self.regex = re.compile(
            r'("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|[a-zA-Z0-9_\-\.\:]{4,})'
        )

    def ingest(self, text: str) -> None:
        matches = self.regex.findall(text)
        with self.lock:
            for m in matches:
                # Clean up quotes if present
                clean = m.strip("\"'")
                if clean and clean not in self.seen:
                    if len(self.tokens) >= self.tokens.maxlen:
                        oldest = self.tokens.popleft()
                        self.seen.discard(oldest)
                    self.tokens.append(clean)
                    self.seen.add(clean)

    def sample(self, rng: random.Random) -> str | None:
        with self.lock:
            if not self.tokens:
                return None
            return rng.choice(self.tokens)
