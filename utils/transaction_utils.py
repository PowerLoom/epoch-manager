from settings.conf import settings
CHAIN_ID = settings.anchor_chain.chain_id

# Default timeout for waiting for transaction receipt (in seconds)
# This prevents indefinite waiting if transaction is stuck
DEFAULT_RECEIPT_TIMEOUT = 300  # 5 minutes


async def write_transaction(w3, address, private_key, contract, function, nonce, gas, *args):
    """ Writes a transaction to the blockchain

    Args:
            w3 (web3.Web3): Web3 object
            address (str): The address of the account
            private_key (str): The private key of the account
            contract (web3.eth.contract): Web3 contract object
            function (str): The function to call
            *args: The arguments to pass to the function

    Returns:
            str: The transaction hash
    """
    # Create the function
    func = getattr(contract.functions, function)
    # Get the transaction
    transaction = await func(*args).build_transaction({
        'from': address,
        'gas': 2000000,
        'gasPrice': w3.to_wei(str(gas), 'gwei'),
        'nonce': nonce,
        'chainId': CHAIN_ID,
    })
    # Sign the transaction
    signed_transaction = w3.eth.account.sign_transaction(
        transaction, private_key=private_key,
    )
    # Send the transaction
    tx_hash = await w3.eth.send_raw_transaction(signed_transaction.rawTransaction)
    # Return transaction hash (keep as bytes/HexBytes for compatibility)
    return tx_hash


async def write_transaction_with_receipt(w3, address, private_key, contract, function, nonce, gas, *args):
    """
    Writes a transaction and returns immediately (fire-and-forget pattern).
    
    Following relayer-py pattern: submit transaction, return tx_hash immediately.
    Receipt waiting happens in background for nonce management.
    
    Args:
        w3 (web3): Web3 object
        address (str): The address of the account
        private_key (str): The private key of the account
        contract (web3.eth.contract): Web3 contract object
        function (str): The function to call
        nonce (int): The nonce for the transaction
        gas (float): Gas price in gwei
        *args: The arguments to pass to the function

    Returns:
        tuple: (transaction_hash, None) - receipt is None because we don't wait for it
        
    Note:
        This follows the relayer pattern where epoch release should NOT block on receipt.
        Nonce management should happen separately based on receipt confirmation.
    """
    # Submit transaction (fire-and-forget) - return immediately
    tx_hash = await write_transaction(
        w3, address, private_key, contract, function, nonce, gas, *args,
    )
    
    # Return tx_hash with None receipt - caller should not block on receipt
    # Receipt waiting and nonce management should happen in background
    return tx_hash, None
