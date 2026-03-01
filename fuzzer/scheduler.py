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
        # Default schedule
        self.mode = "exploration" 

        # Constants for FAST
        self.alpha_rho = 5 # Small constant energy alpha/rho
        self.M = 1000      # Maximum energy cap

        # Variables for Plateau Tracking (ignore for now)
        self.plateau_threshold = plateau_threshold
        self.consecutive_no_gain = 0
        self.total_paths_discovered = 1

    def update_metadata(self, seed_id, path_id, inputs_generated):
        """Updates f(i) based on total inputs generated for a path"""
        self.seed_to_path[seed_id] = path_id
        # Increment f(i) by the amount of energy (mutations) spent
        self.f_i[path_id] = self.f_i.get(path_id, 0) + inputs_generated

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

### Plateau Tracking (ignore)
    def on_new_path_discovered(self):
            """Call this whenever a mutation finds a NEW path ID"""
            self.total_paths_discovered += 1
            self.consecutive_no_gain = 0 # Reset the 'stale' counter
            
            # If we were in FAST mode, maybe we found enough to try Exploration again?
            # Or simply stay in FAST to keep pushing deep.
            print(f"[*] New path found! Total: {self.total_paths_discovered}")

    def on_loop_completed(self, found_new: bool):
        """Call this function at the end of every ChooseNext -> Mutate cycle"""
        if not found_new:
            self.consecutive_no_gain += 1
        
        # Check for plateau
        if self.mode == "exploration" and self.consecutive_no_gain >= self.plateau_threshold:
            self.swap_schedule("fast")
