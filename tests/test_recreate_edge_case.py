

import os
from web3 import Web3


async def test_recreate_edge_case():
    rand_hash = Web3.keccak(text='random')
    print(rand_hash)
    