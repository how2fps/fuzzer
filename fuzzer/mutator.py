import random
import string

#'["foo", {"bar":["baz", null, 1.0, 2]}]'
#'{"__complex__": true, "real": 1, "imag": 2}'
#'{"json":"obj"}'
#need to add json specific ones, and ipv4/ipv6 ones

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
                        walk.append((None, ":"))
                        walk.extend(json_to_walk(value))
            
                        if i < len(items) - 1:
                                        walk.append((None, ","))
                walk.append((None, "}"))

        elif isinstance(data, list):
                walk.append(("VALUE", "["))
                for i, item in enumerate(data):
                        walk.extend(json_to_walk(item))
                        if i < len(items) - 1:
                                walk.append((None, ","))
                walk.append((None, "]"))
        
        elif isinstance(data, str):
                walk.append(("STR_BODY", data)) 
        elif isinstance(data, (int, float)):
                walk.append(("VALUE", str(data)))
        elif data is None:
                walk.append(("VALUE", "null"))
        
        return walk

json_grammar_map = {
        "VALUE": [
                ('{', ["OBJ_BODY"]),
                ('[', ["ARR_BODY"]),
                ('"', ["STR_BODY"]),
                (gen_int, []),
                ('t', ['r', 'u', 'e']),
                ('f', ['a', 'l', 's', 'e'])
                ],
        "OBJ_BODY": [
                ('"', ["STR_BODY", ":", "VALUE", "NEXT_OBJ"]),
                ('}', [])
                ],
        "NEXT_OBJ": [
                (',', ["VALUE_IN_OBJ"]),
                ('}', [])
                ],
        "VALUE_IN_OBJ": [
                ('"', ["STR_BODY", ":", "VALUE", "NEXT_OBJ"])
                ],
        "STR_BODY": [
                (gen_random_str, ["\""]),
                ('"', [])
                ],
        "ARR_BODY": [
                (']', []),
                ('1', ["NEXT_ARR"])
        ],
        "NEXT_ARR": [
                (',', ["VALUE", "NEXT_ARR"]),
                (']', [])
                ]
        }


class Mutator:
        def __init__(self, grammar_map, seed=None):
                self.grammar = grammar_map
                self.start_state = "START"
                self.end_state = "FINAL"
                self.rng = random.Random()
                if seed is not None:
                        self.rng.seed(seed)
                
        def mutate(self, original_walk):
                
                if not original_walk:
                        return self.generate_walk(self.start_state)
                
                random_idx = random.randrange(len(original_walk))
                divergent_state = original_walk[random_idx]
                while divergent_state is None and idx > 0:
                        idx -= 1
                        divergent_state, _ = original_walk[idx]
                new_walk = original_walk[:random_idx]
                new_suffix = self.generate_walk(divergent_state)
                return new_walk + new_suffix


        def generate_walk(self, current_state):
                walk = []
                while current_state!= self.end_state:
                # Pick a random arrow from the current circle
                        terminal, next_state = random.choice(self.grammar[current_state])
                        walk.append((current_state, terminal))
                        current_state = next_state
                return walk


        def unparse(self, walk):
                return "".join([step[1] for step in walk])

        #BASIC BIT MUTATES
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
                
        