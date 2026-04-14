from __future__ import annotations

import random


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
        self.cycle_window = len(operators) * 10
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
        # Dynamic epsilon based on recent coverage success
        total_cycle_successes = sum(self.cycle_success.values())
        if total_cycle_successes == 0:
            # We are stuck. Increase epsilon (explore more), cap at 0.25
            self.epsilon = min(0.25, self.epsilon * 1.1 + 0.01)
        else:
            # We are finding coverage. Decrease epsilon (exploit more), min 0.01
            self.epsilon = max(0.01, self.epsilon * 0.9)

        # Decay history so ancient success doesn't hold weight forever
        decay_factor = 0.85
        for op in self.local_best_efficiencies:
            self.local_best_efficiencies[op] *= decay_factor
        self.global_best_efficiency *= decay_factor

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
