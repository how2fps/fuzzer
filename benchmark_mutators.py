import os
import random
import time
import json
import hashlib
import sys
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Target: run_parser
from parser.parser import run_parser

def load_mutator(module_name):
    # Dynamic import
    import importlib
    mut_module = importlib.import_module(f"mutator.versions.{module_name}")
    return mut_module

def main():
    # Load IPv4 seeds
    seed_file = PROJECT_ROOT / "seed_corpus" / "ipv4-v1.seeds.json"
    with open(seed_file, "r") as f:
        seeds_data = json.load(f)
        # seeds_data is a dictionary with a "seeds" list of objects
        seeds = [s["content"] for s in seeds_data.get("seeds", [])]

    iterations = 2000 # Reduced for faster comparison but enough for PSO
    
    results = []
    
    # Target function wrapper for cidrize
    from parser.parser import run_parser
    
    def target_func(data):
        res = run_parser(input_data=data.encode("utf-8"), target="cidrize")
        return res.get("closed_result", {})

    def run_wrapper(mutator_name, seeds, iterations=1000, mutator_kind="ipv4"):
        print(f"\n[+] Starting benchmark for: {mutator_name}")
        mut_module = load_mutator(mutator_name)
        
        unique_findings = set()
        success_count = 0
        start_time = time.perf_counter()
        
        rng = random.Random(42)
        
        for i in range(iterations):
            if i > 0 and i % 500 == 0:
                print(f"    Iteration {i}/{iterations}...")
                
            seed = rng.choice(seeds)
            try:
                mutated = mut_module.mutate(seed, mutator_kind=mutator_kind, rng=rng)
            except Exception:
                continue
                
            result = target_func(mutated)
            status = result.get("status")
            bug_sig = result.get("bug_signature")
            
            sig_str = f"{status}:{json.dumps(bug_sig, sort_keys=True)}"
            gained_coverage = False
            if sig_str not in unique_findings:
                unique_findings.add(sig_str)
                gained_coverage = True
                success_count += 1
                
            mut_module.handle_feedback(mutated_text=mutated, gained_coverage=gained_coverage)
            
        end_time = time.perf_counter()
        duration = end_time - start_time
        
        return {
            "mutator": mutator_name,
            "iterations": iterations,
            "duration": duration,
            "success_count": success_count,
            "unique_findings": len(unique_findings),
            "speed (execs/s)": iterations / duration if duration > 0 else 0
        }

    # Run Baseline
    results.append(run_wrapper("adaptive_all_baseline", seeds, iterations=iterations))
    
    # Run Experiment
    results.append(run_wrapper("adaptive_all_experiment", seeds, iterations=iterations))
    
    # Summary
    print("\n" + "="*50)
    print("BENCHMARK RESULTS (A/B TEST - CIDRIZE)")
    print("="*50)
    print(f"{'Mutator':<25} | {'Unique':<8} | {'Speed':<10}")
    print("-" * 50)
    for r in results:
        print(f"{r['mutator']:<25} | {r['unique_findings']:<8} | {r['speed (execs/s)']:>8.2f}")
    print("="*50)

if __name__ == "__main__":
    main()
