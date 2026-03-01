# HybridPowerScheduler

The **HybridPowerScheduler** is a dynamic power scheduler bridging traditional "exploration" fuzzing and frequency-based scheduling (FAST)

## How it works

The scheduler operates as a state machine that transitions between two distinct phases based on the feedback it receives from the target program:

### 1. Exploration Mode:

At the start of a fuzzing, the scheduler assigns a constant energy ($E(t_i) = \alpha$) to all seeds

- **Goal:** Rapidly map out the low-hanging fruit and build a diverse corpus of initial inputs without prematurely starving any code paths
- **Benefit:** Prevents starvation, ensuring a path is not ignored just because it's easy to reach
- **Trigger:** Initial state, active as long as new code coverage is found frequently

### 2. FAST Mode

Implements the Exponential Power Schedule formula:

$$E(t_i) = \min \left( \frac{\alpha(i)}{\rho} \cdot \frac{2^{s(i)}}{f(i)}, M \right)$$

- **Goal:** Use the Path Frequency ($f(i)$) to identify rare code paths. It "starves" common, well-explored paths and pours massive mutation power into "cold" paths
- **Benefit:** Dramatically increases the probability of reaching deep, nested logic where complex vulnerabilities hide
- **Trigger:** Activated automatically when a Coverage Plateau is detected

## Hybrid Approach

Choosing a single power schedule often leads to a trade-off between breadth and depth. The hybrid approach was chosen to solve three specific problems:

### 1. Starvation Trap

Aggressive schedules like FAST or Cut-Off Exponential are great at finding deep bugs, but if used too early, can mathematically "mute" a code path
that is hit frequently but still contains many undiscovered sub-branches. By starting with Exploration, the fuzzer sees the "big picture" before digging for deep bugs.

### 2. Overcoming the Plateau

All fuzzers eventually hit a wall where simple mutations on common seeds stop finding new code.

- **Without Hybrid:** The fuzzer spends 99% of its CPU cycles repeating the same tests
- **With Hybrid:** The scheduler detects this stagnation and automatically shifts energy to the rarest seeds in the queue, forcing the fuzzer to explore
  the "dark corners" of the program

### 3. Handling "Gold Mines" (The Breakthrough Logic)

Sometimes, a single rare mutation breaks through (like a checksum or a complex header check) and suddenly reveals a massive new area of the code.

- The HybridPowerScheduler includes Breakthrough Detection. If it finds many new paths while in FAST mode, it resets to Exploration to map out this newly
  discovered area before returning to FAST mode.

## Example Integration

```python
scheduler = HybridPowerScheduler(initial_seeds)

while True:
seed_id = choose_next(seed_q)
energy = scheduler.assign_energy(seed_id)

found_new_path = False
for _ in range(energy):
    mutant = mutate(seed_q[seed_id])
    path_id = execute(mutant)

    scheduler.f_i[path_id] = scheduler.f_i.get(path_id, 0) + 1

    if is_new(path_id):
        scheduler.add_new_seed(mutant, path_id)
        scheduler.on_new_path_discovered()
        found_new_path = True

# Inform scheduler of cycle results
scheduler.on_loop_completed(found_new_path)
```
