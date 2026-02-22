import random


#'["foo", {"bar":["baz", null, 1.0, 2]}]'
#'{"__complex__": true, "real": 1, "imag": 2}'
#'{"json":"obj"}'
#need to add json specific ones, and ipv4/ipv6 ones
class Mutator:
        def __init__(self):
                self.interesting_8 = 0

        def mutate(self, data: bytearray) -> bytearray:
                res = data[:]
                if not res: return bytearray(b"0")
        
                for _ in range(random.randint(1, 10)):
                        method = random.choice([
                                self.bit_flip, self.arithmetic, self.interesting_value, 
                                self.delete_block, self.clone_block
                        ])
                        res = method(res)
                return res

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
                
        