import random


#'["foo", {"bar":["baz", null, 1.0, 2]}]'
#'{"__complex__": true, "real": 1, "imag": 2}'
#'{"json":"obj"}'
#need to add json specific ones, and ipv4/ipv6 ones

json_grammar_map = {
    "START":      ,
    "OBJ_BODY":   ,
    "VAL_IN_OBJ": ,
    "NESTED_OBJ": , 
    "OBJ_END":    ,
    "FINAL":      
}
class Mutator:
        def __init__(self, grammar_map):
                self.grammar = grammar_map
                self.start_state = "START"
                self.end_state = "FINAL"
                
        def mutate(self, original_walk):
                if not original_walk:
                        return self.generate_walk(self.start_state)
                
                random_idx = random.randrange(len(original_walk))
                divergent_state = original_walk[random_idx]
                
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
                
        