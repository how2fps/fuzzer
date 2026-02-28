#'["foo", {"bar":["baz", null, 1.0, 2]}]'
#'{"__complex__": true, "real": 1, "imag": 2}'
#'{"json":"obj"}'
#need to add json specific ones, and ipv4/ipv6 ones
import json
import random
import string


def print_pretty_json(raw_string):
    try:
        # 1. Try to load the string as a real JSON object
        data = json.loads(raw_string)
        # 2. If it works, print the dictionary/list structure
        print("--- PARSED OBJECT ---")
        print(json.dumps(data, indent=4))
    except json.JSONDecodeError as e:
        print(f"--- INVALID JSON (Couldn't parse) ---")
        print(f"Raw string: {raw_string}")
        print(f"Error: {e}")
        
def gen_random_str(rng):
        length = rng.randint(1, 25)
        body = "".join(rng.choices(string.ascii_letters + string.digits, k=length))
        return body

def gen_int(rng):
        return str(rng.choice([0, -1, 2147483647, rng.randint(0, 1000)]))

def json_to_walk(data):
        walk = []
        if isinstance(data, dict):
                walk.append(("VALUE", "{"))
                items = list(data.items())
                for i, (key, value) in enumerate(items):
                        walk.append(("STR_BODY", key))
                        walk.append(("OBJ_BODY", ":")) # Use a real state from your map
                        walk.extend(json_to_walk(value))
                        if i < len(items) - 1:
                                walk.append(("NEXT_OBJ", ","))
                walk.append(("FINAL", "}"))
        elif isinstance(data, str):
                walk.append(("STR_BODY", data))
        elif isinstance(data, (int, float)):
                walk.append(("VALUE", str(data)))
        return walk

def gen_quoted_str(rng):
    return '"' + gen_random_str(rng) + '"'

json_grammar_map = {
    "VALUE": [
        ('{', "OBJ_BODY"), ('{', "OBJ_BODY"),
        ('[', "ARR_BODY"), ('[', "ARR_BODY"),
        (gen_int, "FINAL"), ('true', "FINAL")
    ],
    "VAL_IN_OBJ": [
        (gen_int, "OBJ_CONTINUE"),
        ('true', "OBJ_CONTINUE"),
        ('{', "OBJ_BODY"), ('{', "OBJ_BODY"),
        ('[', "ARR_BODY"), ('[', "ARR_BODY") 
    ],    
    "OBJ_BODY": [('"', "STR_START")], 
    "STR_START": [(gen_random_str, "STR_END")],
    "STR_END": [('"', "COLON")],
    "COLON": [(':', "VAL_IN_OBJ")],
    
    "OBJ_BODY_NESTED": [('"', "STR_START_NESTED")],
    "STR_START_NESTED": [(gen_random_str, "STR_END_NESTED")],
    "STR_END_NESTED": [('"', "COLON_NESTED")],
    "COLON_NESTED": [(':', "VAL_IN_OBJ_NESTED")],
    "VAL_IN_OBJ_NESTED": [(gen_int, "OBJ_CONTINUE")], 
    
    "OBJ_CONTINUE": [('}', "FINAL"), (',', "OBJ_BODY")],
    
    "ARR_BODY": [(']', "FINAL"), (gen_int, "ARR_NEXT")],
    "ARR_NEXT": [(',', "ARR_VAL"), (']', "FINAL")],
    "ARR_VAL": [(gen_int, "ARR_NEXT")]
}


class Mutator:
        def __init__(self, grammar_map, seed=None):
                self.grammar = grammar_map
                self.start_state = "VALUE"
                self.end_state = "FINAL"
                self.rng = random.Random()
                if seed is not None:
                        self.rng.seed(seed)
                
                
        def havoc(self, walk, corpus):
                if not walk: return self.generate_walk(self.start_state)
        
                mutated = list(walk)
                num_mutations = 1 << random.randint(1, 4) # Reduced for testing
        
                for _ in range(num_mutations):
                    strategies = [self.mutate_random]
                    if len(corpus) > 1:
                        strategies.append(self.mutate_splice)
                    
                    strategy = random.choice(strategies)
                    
                    if strategy == self.mutate_splice:
                        other = random.choice(corpus)
                        mutated = self.mutate_splice(mutated, other)
                    else:
                        mutated = self.mutate_random(mutated)
                return mutated                
        
        def mutate_random(self, walk):
                if not walk: return self.generate_walk(self.start_state)
                split_idx = random.randrange(len(walk))
                state_to_diverge_from = walk[split_idx][0] 
                return walk[:split_idx] + self.generate_walk(state_to_diverge_from)
    
        def mutate_splice(self, walk1, walk2):
                states1 = {step: i for i, step in enumerate(walk1) if step is not None}
                common = [j for j, step in enumerate(walk2) if step in states1]
                if not common: return walk1
                w2_idx = random.choice(common)
                shared_state = walk2[w2_idx]
                w1_idx = states1[shared_state]
                return walk1[:w1_idx] + walk2[w2_idx:]                
        
        def generate_walk(self, current_state, max_depth=30):
                walk = []
                while current_state != self.end_state and current_state in self.grammar and max_depth > 0:
                        choices = self.grammar[current_state]
                        term_choice, next_state = random.choice(choices)

                        terminal = term_choice(self.rng) if callable(term_choice) else term_choice
                        walk.append((current_state, terminal))

                        current_state = next_state
                        max_depth -= 1

                if current_state != self.end_state:
                        if "OBJ" in current_state: walk.append(("FORCE_CLOSE", "}"))
                        elif "ARR" in current_state: walk.append(("FORCE_CLOSE", "]"))

                return walk
    
        def unparse(self, walk):
            return "".join([str(step[1]) for step in walk])

        def mutate(self, walk):
                return self.havoc(walk, [])
        
        
                
        def bit_flip(self, data):
                idx = random.randrange(len(data))
                data[idx] ^= (1 << random.randrange(8))
                return data

        def arithmetic(self, data):
                idx = random.randrange(len(data))

        def interesting_value(self, data):
                idx = random.randrange(len(data))
                

        def delete_block(self, data):
                if len(data) < 2: return data
                idx = random.randrange(len(data))
               

        def clone_block(self, data):
                idx = random.randrange(len(data))
                
        
        
mutator = Mutator(json_grammar_map)

seed_walk = mutator.generate_walk("VALUE")
mutated_walk = mutator.mutate(seed_walk)

final_str = mutator.unparse(mutated_walk)

print_pretty_json(final_str)