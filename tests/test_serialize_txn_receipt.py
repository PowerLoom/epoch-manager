import asyncio
import json
import os
from typing import Optional
from pydantic import BaseModel

from web3 import Web3



class GenericTxnIssue(BaseModel):
    accountAddress: str
    issueType: str
    projectId: Optional[str]
    epochBegin: Optional[str]
    epochId: Optional[str]
    extra: Optional[str] = ''

async def test_serialize_txn_receipt():
    w3 = Web3(Web3.HTTPProvider(os.environ['RPC_URL']))
    block_number = w3.eth.block_number
    latest_block = w3.eth.get_block(block_number)
    first_txn_in_block = latest_block.transactions[0]
    txn_receipt = w3.eth.get_transaction_receipt(first_txn_in_block)
    print(txn_receipt)
    serialized = Web3.to_json(txn_receipt)
    # serialized = json.dumps(txn_receipt, cls=Web3.)
    print(serialized)

    issue = GenericTxnIssue( accountAddress='0x',
                    epochBegin=1,
                    issueType='EpochReleaseError',
                    extra=str({'error': f"Transaction failed with status: {txn_receipt['status']}"}),
                )
    
    msg = issue.dict()
    serialized = json.dumps(msg)
    print(serialized)
    r = json.dumps({'begin': 10101, 'end': 10121})
    print(r)


if __name__ == '__main__':
    asyncio.run(test_serialize_txn_receipt())