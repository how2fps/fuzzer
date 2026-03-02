import math
import random

class HybridPowerScheduler:
    def __init__(self, initial_seeds, plateau_threshold=500, breakthrough_limit=10):
        # Seed Queue 
        self.corpus = {f"seed_{i}": data for i, data in enumerate(initial_seeds)}
        # s(i): number of times seed ti is chosen from the queue
        self.s_i = {sid: 0 for sid in self.corpus}
        # f(i): number of inputs generated that exercise path i
        self.f_i = {} 
        # Mapping seed IDs to their specific path IDs
        self.seed_to_path = {sid: "initial" for sid in self.corpus}
        
        # List of available schedules to choose from
        self.modes = ["exploration", "fast"]

        # Constants for FAST
        self.alpha_rho = 5 # Small constant energy alpha/rho
        self.M = 1000      # Maximum energy cap

        # State Management
        self.mode = "exploration"  # Default schedule 
        self.plateau_threshold = plateau_threshold  # Cycles with no gain before FAST
        self.breakthrough_limit = breakthrough_limit # Finds in FAST before Exploration

        self.consecutive_no_gain = 0
        self.finds_in_fast_mode = 0
        self.total_paths_discovered = 1

    def schedule_exploration(self, seed_id):
        """Balanced schedule: constant energy to avoid early starvation"""
        return 100 # Constant energy: alpha(i)

    def schedule_fast(self, seed_id):
        """Exponential (FAST) schedule: E(ti) = min((alpha/rho) * (2^s(i)/f(i)), M)"""
        path_id = self.seed_to_path[seed_id]
        s = self.s_i[seed_id]
        f = self.f_i.get(path_id, 1) # Avoid division by zero
        
        # Calculate energy using the FAST formula
        energy = self.alpha_rho * (math.pow(2, s) / f)
        return int(min(energy, self.M))

    def swap_schedule(self, new_mode):
        """For swapping between power schedules"""
        if new_mode in self.modes:
            print(f"--- Transitioning to {new_mode.upper()} schedule ---")
            self.mode = new_mode

    def assign_energy(self, seed_id):
        """Delegates energy calculation based on current mode."""
        self.s_i[seed_id] += 1 # Increment s(i)
        
        if self.mode == "exploration":
            return self.schedule_exploration(seed_id)
        elif self.mode == "fast":
            return self.schedule_fast(seed_id)

    def on_new_path_discovered(self):
            """Call this whenever a mutation finds a NEW path"""
            self.total_paths_discovered += 1
            self.consecutive_no_gain = 0 # Breakthrough resets the plateau counter
            
            if self.mode == "fast":
                self.finds_in_fast_mode += 1
                # If find a 'gold mine' of new paths, go back to Exploration
                if self.finds_in_fast_mode >= self.breakthrough_limit:
                    print(f"[!] Breakthrough of {self.breakthrough_limit} paths! Resetting to Exploration.")
                    self.swap_schedule("exploration")
                    self.finds_in_fast_mode = 0
                    
            print(f"[*] New path found! Total unique paths: {self.total_paths_discovered}")

    def on_loop_completed(self, found_new_this_cycle: bool):
        """Call this function at the end of every ChooseNext -> Mutate cycle to track plateau progress"""
        if not found_new_this_cycle:
            self.consecutive_no_gain += 1
        
        # If we've hit the limit of boring cycles, get aggressive
        if self.mode == "exploration" and self.consecutive_no_gain >= self.plateau_threshold:
            self.swap_schedule("fast")

    def add_new_seed(self, seed_data, path_id):
        """Adds a newly discovered seed to the corpus."""
        new_id = f"seed_{len(self.corpus)}"
        self.corpus[new_id] = seed_data
        self.seed_to_path[new_id] = path_id
        self.s_i[new_id] = 0
        return new_id