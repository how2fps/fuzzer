import random
import json
from mutator.mutator import mutate_json, mutate_ip, _GLOBAL_FUZZER

def test_json_mutations():
    print("--- Testing JSON Mutations ---")
    original = '{"key": "value", "count": 10}'
    for i in range(20):
        mutated = mutate_json(original)
        print(f"[{i:02d}] {mutated}")

def test_ip_mutations():
    print("\n--- Testing IP Mutations ---")
    original = "192.168.1.1/24"
    for i in range(20):
        mutated = mutate_ip(original)
        print(f"[{i:02d}] {mutated}")

def test_adaptive_strategy():
    print("\n--- Testing Adaptive Strategy ---")
    # Simulate many mutations and "successes" for a specific operator
    # to see if weights shift.
    strategy = _GLOBAL_FUZZER.strategies["json"]
    target_op = "mutate_keys"
    
    print(f"Initial probabilities: {strategy.get_probabilities()}")
    
    for _ in range(100):
        op = strategy.select_operator(random.Random())
        # If it's our target, simulate a success
        strategy.update_score(op, op == target_op)
    
    print(f"Final probabilities: {strategy.get_probabilities()}")

if __name__ == "__main__":
    test_json_mutations()
    test_ip_mutations()
    test_adaptive_strategy()
