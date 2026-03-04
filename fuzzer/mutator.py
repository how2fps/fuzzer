import json
import os
import random
import string


def print_pretty_json(raw_string):
        try:
                data = json.loads(raw_string)
                print("PARSED OBJECT: ")
                print(json.dumps(data, indent=4))
        except json.JSONDecodeError as e:
                print(f"INVALID JSON (Couldn't parse): ")
                print(f"Raw string: {raw_string}")
                print(f"Error: {e}")
        
def gen_random_str(rng):
        length = rng.randint(1, 25)
        body = "".join(rng.choices(string.ascii_letters + string.digits, k=length))
        return body

def gen_int(rng):
        edge_cases = [
            0,
            -1,
            1,
            2147483647,
            -2147483648,
            4294967295,
            9223372036854775807,
            -9223372036854775808,
            rng.randint(-100000, 100000)
        ]
        return str(rng.choice(edge_cases))
    
def json_to_walk(data):
        walk = []
        if isinstance(data, dict):
                walk.append(("OBJ", "{"))
                items = list(data.items())
                for i, (key, value) in enumerate(items):
                        walk.append(("KEY", key)) 
                        walk.extend(json_to_walk(value))
                        if i < len(items) - 1:
                                walk.append(("NEXT", ","))
                walk.append(("END_OBJ", "}"))
        
        elif isinstance(data, list):
                walk.append(("ARR", "["))
                for i, value in enumerate(data):
                        walk.extend(json_to_walk(value))
                        if i < len(data) - 1:
                                walk.append(("NEXT_ARR", ","))
                walk.append(("END_ARR", "]"))
        
        elif isinstance(data, str):
                walk.append(("STR_BODY", data))
        
        elif isinstance(data, bool):
                walk.append(("BOOL", "true" if data else "false"))  
        elif isinstance(data, (int, float)):
                walk.append(("VAL", str(data)))
        
        elif data is None:
                walk.append(("VAL", "null"))
        
        return walk


grammar = {
        "VALUE": [("OBJ", "END"), ("ARR", "END")],
        "OBJ": [("KEY", "VAL"), ("KEY", "VAL_AND_NEXT")],
        "VAL": [(gen_int, "END"), ("true", "END"), ("false", "END")],
        "VAL_AND_NEXT": [(gen_int, "OBJ"), ("true", "OBJ"), ("false", "OBJ")],
        "END": []
}

class Mutator:
        def __init__(self, grammar_map, seed=None):
                self.grammar = grammar_map
                self.start_state = "VALUE"
                self.end_state = "FINAL"
                self.rng = random.Random()
                if seed is not None:
                        self.rng.seed(seed)

        def load_corpus_from_dir(self, directory_path):
                corpus = []
                if not os.path.exists(directory_path):
                        print(f"Directory not found: {directory_path}")
                        return corpus

                for filename in os.listdir(directory_path):
                        if filename.endswith(".json"):
                                file_path = os.path.join(directory_path, filename)
                                try:
                                        with open(file_path, 'r') as f:
                                                data = json.load(f)
                                                corpus.append((filename, json_to_walk(data)))
                                                print(f"Loaded: {filename}")
                                except (json.JSONDecodeError, IOError) as e:
                                        print(f"Skipping {filename}: {e}")
                return corpus

        def havoc(self, walk, corpus):
                if not walk:
                        return []
                mutated = list(walk)
                num_mutations = 1 << random.randint(0, 3)

                for _ in range(num_mutations):
                        strategies = [
                                self.mutate_string_terminal,
                                self.mutate_int_terminal,
                                self.delete_block,
                                self.clone_block,
                        ]
                        if len(corpus) > 1:
                                strategies.append(lambda w: self.mutate_splice(w, random.choice(corpus)))

                        strategy = random.choice(strategies)
                        result = strategy(mutated)
                        if result:
                                mutated = result

                return mutated
    
        def mutate_splice(self, walk1, walk2):
                states1 = {step: i for i, step in enumerate(walk1) if step is not None}
                common = [j for j, step in enumerate(walk2) if step in states1]
                if not common: return walk1
                w2_idx = random.choice(common)
                shared_state = walk2[w2_idx]
                w1_idx = states1[shared_state]
                return walk1[:w1_idx] + walk2[w2_idx:]
        
  
        def generate_walk(self, current_state, max_depth=50):
            walk = []
            stack = [] 
        
            while current_state != self.end_state and current_state in self.grammar:
                choices = self.grammar[current_state]

                if not stack:
                    choices = [c for c in choices if c[0] not in ['}', ']']]
                else:
                    expected_closer = stack[-1]
                    forbidden = {'}': ']', ']': '}'}[expected_closer]
                    choices = [c for c in choices if c[0] != forbidden]

                term_choice, next_state = random.choice(choices)
                terminal = term_choice(self.rng) if callable(term_choice) else term_choice

                if terminal == '{': stack.append('}')
                elif terminal == '[': stack.append(']')
                elif terminal == '}' or terminal == ']':
                    if stack: stack.pop()

                walk.append((current_state, terminal))
                current_state = next_state

            while stack:
                walk.append(("FORCE_CLOSE", stack.pop()))

            return walk
        
        def unparse(self, walk):
            result = []
            for state, val in walk:

                if state == 'OBJ': result.append('{')
                elif state == 'END_OBJ': result.append('}')
                elif state == 'ARR': result.append('[')
                elif state == 'END_ARR': result.append(']')
                elif state == 'KEY': result.append(f'"{val}":')
                elif state in ['NEXT', 'NEXT_ARR']: result.append(',')


                elif state == 'BOOL': result.append(val)
                elif state == 'STR_BODY': result.append(f'"{val}"')
                elif state == 'VAL': result.append(str(val))
                elif state == 'FORCE_CLOSE':
                        result.append(val)

                else: result.append(str(val))
        
            s = "".join(result)
            s = s.replace(",}", "}").replace(",]", "]")
            return s

        def mutate(self, walk):
                return self.havoc(walk, [])

        _STR_STATES = {'KEY', 'STR_BODY'}
        _INT_STATES = {'VAL'}

        def mutate_string_terminal(self, walk):
                """Randomly mutate one string terminal: replace, flip a char, append, or empty it."""
                idxs = [i for i, (s, _) in enumerate(walk) if s in self._STR_STATES]
                if not idxs:
                        return walk
                idx = random.choice(idxs)
                state, old_val = walk[idx]
                op = random.randint(0, 3)
                if op == 0:
                        new_val = gen_random_str(self.rng)
                elif op == 1 and old_val:
                        i = self.rng.randrange(len(old_val))
                        new_char = self.rng.choice(string.ascii_letters + string.digits)
                        new_val = old_val[:i] + new_char + old_val[i+1:]
                elif op == 2:
                        new_val = old_val + gen_random_str(self.rng)
                else:
                        new_val = ""
                mutated = list(walk)
                mutated[idx] = (state, new_val)
                return mutated

        def mutate_int_terminal(self, walk):
                """Swap one integer terminal with an interesting boundary value."""
                idxs = [
                        i for i, (s, t) in enumerate(walk)
                        if s in self._INT_STATES and str(t) not in ('{', '[', 'true', 'false')
                ]
                if not idxs:
                        return walk
                idx = random.choice(idxs)
                state = walk[idx][0]
                mutated = list(walk)
                mutated[idx] = (state, gen_int(self.rng))
                return mutated

        def delete_block(self, walk):
                idxs = [i for i, (s, _) in enumerate(walk) if s in self._STR_STATES]
                if not idxs:
                        idxs = [
                                i for i, (s, t) in enumerate(walk)
                                if s in self._INT_STATES and str(t) not in ('{', '[', 'true', 'false')
                        ]
                if not idxs:
                        return walk
                idx = random.choice(idxs)
                state, terminal = walk[idx]
                mutated = list(walk)
                mutated[idx] = (state, "" if state in self._STR_STATES else "0")
                return mutated

        def clone_block(self, walk):
                str_idxs = [i for i, (s, _) in enumerate(walk) if s in self._STR_STATES]
                int_idxs = [
                        i for i, (s, t) in enumerate(walk)
                        if s in self._INT_STATES and str(t) not in ('{', '[', 'true', 'false')
                ]
                mutated = list(walk)
                for pool in (str_idxs, int_idxs):
                        if len(pool) >= 2:
                                src, dst = random.sample(pool, 2)
                                mutated[dst] = (mutated[dst][0], mutated[src][1])
                                return mutated
                return walk
        
        
mutator = Mutator(grammar)

corpus_dir = r"C:\Users\heart\TestingProject\corpus"
my_corpus = mutator.load_corpus_from_dir(corpus_dir)

if my_corpus:
        seed_name, seed_walk = random.choice(my_corpus)
        print(f"\nSeed: {seed_name}")

        walks_only = [w for _, w in my_corpus]
        mutated_walk = mutator.havoc(seed_walk, walks_only)
        

        print_pretty_json(mutator.unparse(mutated_walk))