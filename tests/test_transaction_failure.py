# Import necessary libraries
import asyncio
import json
from unittest.mock import AsyncMock

from epoch_generator import EpochGenerator

# Assuming other necessary imports are already there as per your original script

# Mock function to simulate write_transaction behavior

failed = False
async def mock_write_transaction(web3, validator_address, validator_private_key, contract, function_name, nonce, begin, end, simulate_failure=False):
    if simulate_failure:
        raise Exception("Simulated transaction failure")
    else:
        return "0xmocktransactionhash"

# Mock function to simulate write_transaction_with_receipt behavior
async def mock_write_transaction_with_receipt(web3, validator_address, validator_private_key, contract, function_name, nonce, begin, end, simulate_failure=False):
    if simulate_failure:
        return '0xmocktransactionhash', {'status': 0, 'gasUsed': 0, 'gas': 0, 'cumulativeGasUsed': 0, 'logs': []}
    else:
        # Simulate a successful transaction receipt
        return "0xmocktransactionhash", {"status": 1}

# Your EpochGenerator class with mocked transaction functions for testing
class EpochGeneratorMock(EpochGenerator):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Replace the real transaction functions with mocks
        self.write_transaction = AsyncMock(side_effect=mock_write_transaction)
        self.write_transaction_with_receipt = AsyncMock(side_effect=mock_write_transaction_with_receipt)

    # Override or add any additional methods if necessary for your tests

# Example usage
async def main():
    # Initialize your mock generator instead of the real one
    epoch_generator_mock = EpochGeneratorMock()

    # Customize the mock behavior for your tests, e.g., simulate transaction failure
    epoch_generator_mock.write_transaction.side_effect = lambda *args, **kwargs: mock_write_transaction(*args, **kwargs, simulate_failure=True)
    epoch_generator_mock.write_transaction_with_receipt.side_effect = lambda *args, **kwargs: mock_write_transaction_with_receipt(*args, **kwargs, simulate_failure=True)

    # Run your test scenario
    await epoch_generator_mock.run()

if __name__ == "__main__":
    asyncio.run(main())
