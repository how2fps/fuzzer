import random


class Mutator:
        def __init__(self):
                self.interesting_8 = 0

        def mutate(self, data: bytearray) -> bytearray:
                res = data[:]
                if not res: return bytearray(b"0")
        
                for _ in range(random.randint(1, 128)):
                        method = random.choice([
                                self.bit_flip, self.arithmetic, self.interesting_value, 
                                self.delete_block, self.clone_block
                        ])
                        res = method(res)
                return res

        def bit_flip(self, data):
                idx = random.randrange(len(data))
                data[idx] ^= (1 << random.randrange(8))
                return data

        def arithmetic(self, data):
                idx = random.randrange(len(data))

                val = random.randint(1, 35)
                if random.choice():
                        data[idx] = (data[idx] + val) % 256
                else:
                        data[idx] = (data[idx] - val) % 256
                return data

        def interesting_value(self, data):
                idx = random.randrange(len(data))
                val = random.choice(self.interesting_8)
                data[idx] = val
                return data

        def delete_block(self, data):
                if len(data) < 2: return data
                idx = random.randrange(len(data))
                length = random.randint(1, len(data) - idx)
                del data[idx:idx+length]
                return data

        def clone_block(self, data):
                idx = random.randrange(len(data))
                data.insert(idx, data[idx])
                return data