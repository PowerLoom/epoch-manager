import asyncio
import json
import resource
import time
import traceback
from multiprocessing import Process
from signal import SIGINT
from signal import signal
from signal import SIGQUIT
from signal import SIGTERM

import uvloop
from aiohttp import ClientTimeout as AiohttpClientTimeout
from httpx import AsyncClient
from httpx import AsyncHTTPTransport
from httpx import Limits
from httpx import Timeout
from tenacity import retry
from tenacity import retry_if_exception_type
from tenacity import stop_after_attempt
from tenacity import wait_random_exponential
from web3 import AsyncHTTPProvider
from web3 import AsyncWeb3
from web3 import Web3

from data_models import GenericTxnIssue
from exceptions import GenericExitOnSignal
from helpers.message_models import RPCNodesObject
from helpers.rpc_helper import ConstructRPC
from settings.conf import settings
from utils.default_logger import logger
from utils.helpers import chunks
from utils.notification_utils import send_failure_notifications
from utils.transaction_utils import write_transaction
from utils.transaction_utils import write_transaction_with_receipt
protocol_state_contract_address = settings.protocol_state_address
data_market_address = settings.data_market_address

# load abi from json file and create contract object
with open('utils/static/abi.json', 'r') as f:
    abi = json.load(f)

# Configure timeout with granular settings for better control
# total: total timeout for entire operation
# connect: timeout for establishing connection
# sock_read: timeout for reading data from socket
# sock_connect: timeout for socket connection
timeout_seconds = settings.anchor_chain.rpc.request_time_out
w3 = AsyncWeb3(
    AsyncHTTPProvider(
        settings.anchor_chain.rpc.full_nodes[0].url,
        request_kwargs={
            'timeout': AiohttpClientTimeout(
                total=timeout_seconds,
                connect=min(timeout_seconds, 10),  # Connection timeout (max 10s)
                sock_read=timeout_seconds,  # Socket read timeout
                sock_connect=min(timeout_seconds, 10),  # Socket connect timeout (max 10s)
            )
        },
    )
)
protocol_state_contract = w3.eth.contract(
    address=protocol_state_contract_address, abi=abi,
)


class EpochGenerator:

    def __init__(self, name='EpochGenerator'):
        self._logger = logger.bind(module=name)
        self._shutdown_initiated = False
        self._end = None
        self._nonce = -1
        self._force_skip_nonce = -1  # only for forceSkipEpoch (force_consensus identity)
        self._async_transport = None
        self._client = None
        self.release_counter = 0
        self._force_tx = False
        self.gas = settings.anchor_chain.default_gas_in_gwei
        self.high_gas = settings.anchor_chain.default_gas_in_gwei*2
        
        # Adaptive polling configuration (similar to epochsyncer)
        self.MIN_POLLING_INTERVAL = 0.1  # 100ms - fast polling for catch-up
        self.MAX_POLLING_INTERVAL = 5.0  # 5s - slow polling when caught up
        self.current_polling_interval = 1.0  # Start with 1s
        self.ADAPTIVE_POLLING = True
        
        # Gap detection threshold
        self.GAP_THRESHOLD = 10  # blocks - if gap >= this, skip catch-up and start fresh
        self.GAP_OFFSET = 1  # Start from current_head - offset when gap is too large

    @retry(
        reraise=True,
        retry=retry_if_exception_type((asyncio.TimeoutError, OSError, ConnectionError)),
        wait=wait_random_exponential(multiplier=1, max=10),
        stop=stop_after_attempt(settings.anchor_chain.rpc.retry),
    )
    async def setup(self):
        self._logger.debug(
            'Fetching nonce from anchor RPC: {}',
            settings.anchor_chain.rpc.full_nodes[0].url,
        )
        self._nonce = await w3.eth.get_transaction_count(
            settings.validator_epoch_address,
        )
        self._force_skip_nonce = await w3.eth.get_transaction_count(
            settings.force_consensus_address,
        )
        await self._init_httpx_client()

    async def _init_httpx_client(self):
        if self._async_transport is not None:
            return
        self._async_transport = AsyncHTTPTransport(
            limits=Limits(
                max_connections=100,
                max_keepalive_connections=50,
                keepalive_expiry=None,
            ),
        )
        self._client = AsyncClient(
            timeout=Timeout(timeout=30.0),
            follow_redirects=False,
            transport=self._async_transport,
        )

    async def _adaptive_polling_adjustment(self, blocks_processed: int, processing_time: float):
        """
        Adjust polling interval based on performance metrics.
        
        Similar to epochsyncer's adaptive polling - adjusts interval based on throughput.
        High throughput (> 10 blocks/s) -> decrease interval (poll more frequently)
        Low throughput (< 1 block/s) -> increase interval (poll less frequently)
        """
        if not self.ADAPTIVE_POLLING:
            return
        
        # Calculate blocks per second
        blocks_per_second = blocks_processed / max(processing_time, 0.001)
        
        # Adjust polling interval based on throughput
        if blocks_per_second > 10:  # High throughput - poll more frequently
            self.current_polling_interval = max(
                self.current_polling_interval * 0.8,
                self.MIN_POLLING_INTERVAL
            )
        elif blocks_per_second < 1:  # Low throughput - poll less frequently
            self.current_polling_interval = min(
                self.current_polling_interval * 1.5,
                self.MAX_POLLING_INTERVAL
            )
        
        self._logger.debug(
            'Adaptive polling: {:.2f} blocks/s, interval: {:.2f}s',
            blocks_per_second,
            self.current_polling_interval
        )

    def _generic_exit_handler(self, signum, sigframe):
        if signum in [SIGINT, SIGTERM, SIGQUIT] and not self._shutdown_initiated:
            self._shutdown_initiated = True
            raise GenericExitOnSignal

    @retry(
        reraise=True,
        retry=retry_if_exception_type(Exception),
        wait=wait_random_exponential(multiplier=1, max=10),
        stop=stop_after_attempt(settings.anchor_chain.rpc.retry),
    )
    async def _fetch_epoch_from_contract(self) -> int:
        """Fetch the next epoch to release from the contract."""
        last_epoch_data = await protocol_state_contract.functions.currentEpoch(Web3.to_checksum_address(data_market_address)).call()
        if last_epoch_data[1]:
            self._logger.debug(
                'Found last epoch block : {} in contract.', last_epoch_data[
                    1
                ],
            )
            begin_block_epoch = last_epoch_data[1] + 1
            return begin_block_epoch
        else:
            self._logger.debug(
                'No last epoch block found in contract.',
            )
            return -1

    async def run(self):
        await self.setup()

        last_contract_epoch = await self._fetch_epoch_from_contract()
        if last_contract_epoch != -1:
            begin_block_epoch = last_contract_epoch
        else:
            begin_block_epoch = settings.ticker_begin_block if settings.ticker_begin_block else 0
        for signame in [SIGINT, SIGTERM, SIGQUIT]:
            signal(signame, self._generic_exit_handler)

        # waiting to release epoch chunks every half of block time
        sleep_secs_between_chunks = settings.chain.epoch.block_time // 2

        rpc_obj = ConstructRPC(network_id=settings.chain.chain_id)
        rpc_urls = []
        for node in settings.chain.rpc.full_nodes:
            self._logger.debug('node {}', node.url)
            rpc_urls.append(node.url)
        rpc_nodes_obj = RPCNodesObject(
            NODES=rpc_urls,
            RETRY_LIMIT=settings.chain.rpc.retry,
        )
        self._logger.debug('Starting {}', Process.name)

        if settings.epoch_release_start_timestamp and not begin_block_epoch:
            begin_block_epoch = await self._wait_and_release_first_epoch(
                rpc_obj=rpc_obj,
                rpc_nodes_obj=rpc_nodes_obj,
            )
            if not begin_block_epoch:
                self._logger.error(
                    'Unable to release first epoch on time. Exiting...',
                )
                return

        while True:
            try:
                cur_block = rpc_obj.rpc_eth_blocknumber(
                    rpc_nodes=rpc_nodes_obj,
                )
            except Exception as ex:
                self._logger.error(
                    'Unable to fetch latest block number due to RPC failure {}. Retrying after {} seconds.',
                    ex,
                    settings.chain.epoch.block_time,
                )
                await asyncio.sleep(settings.chain.epoch.block_time)
                continue
            else:
                self._logger.debug('Got current head of chain: {}', cur_block)
                processing_start_time = time.time()
                
                if not begin_block_epoch:
                    self._logger.debug('Begin of epoch not set')
                    begin_block_epoch = cur_block
                    self._logger.debug(
                        'Set begin of epoch to current head of chain: {}', cur_block,
                    )
                    self._logger.debug(
                        'Sleeping for: {} seconds', settings.chain.epoch.block_time,
                    )
                    await asyncio.sleep(settings.chain.epoch.block_time)
                else:
                    end_block_epoch = cur_block - settings.chain.epoch.head_offset
                    
                    # Calculate block gap: distance from current chain head to begin_block_epoch
                    # This detects if we're falling behind the chain head
                    gap_from_head = cur_block - begin_block_epoch  # Gap from actual chain head
                    block_gap = end_block_epoch - begin_block_epoch + 1
                    
                    # Log gap status for debugging (log if gap >= 5 blocks)
                    if gap_from_head >= 5:
                        self._logger.warning(
                            'Gap detected: {} blocks behind chain head (current: {}, begin_block: {}, end_block: {})',
                            gap_from_head, cur_block, begin_block_epoch, end_block_epoch
                        )
                    
                    # Gap detection with threshold-based catch-up (runs every poll)
                    # Covers: (1) restart with contract far behind chain, (2) mid-run fall-behind (e.g. network/RPC issues)
                    use_force_skip = False
                    force_skip_enabled = getattr(settings.chain, 'force_skip_epoch', False)
                    
                    # Use gap_from_head for detection (more accurate than block_gap which includes offset)
                    if gap_from_head >= self.GAP_THRESHOLD and force_skip_enabled:
                        # Large gap detected - use forceSkipEpoch to skip to current head
                        # forceSkipEpoch allows non-sequential epoch release (requires owner permission)
                        # This prevents overwhelming snapshotter nodes with massive catch-up
                        try:
                            last_contract_epoch = await self._fetch_epoch_from_contract()
                            if last_contract_epoch != -1:
                                contract_epoch_end = last_contract_epoch
                                # Calculate target epoch that's a multiple of EPOCH_SIZE from currentEpoch.end
                                # For EPOCH_SIZE == 1, we can jump to any block
                                if settings.chain.epoch.height == 1:
                                    # Jump directly to current head - offset
                                    begin_block_epoch = cur_block - settings.chain.epoch.head_offset - self.GAP_OFFSET
                                    end_block_epoch = cur_block - settings.chain.epoch.head_offset
                                else:
                                    # Calculate valid epoch that's a multiple of epoch_height from contract_epoch_end
                                    blocks_to_skip = end_block_epoch - contract_epoch_end
                                    epochs_to_skip = blocks_to_skip // settings.chain.epoch.height
                                    begin_block_epoch = contract_epoch_end + (epochs_to_skip * settings.chain.epoch.height) + 1
                                    end_block_epoch = begin_block_epoch + settings.chain.epoch.height - 1
                                
                                use_force_skip = True
                                self._logger.warning(
                                    'Large block gap detected: {} blocks from chain head (>= threshold {}). '
                                    'force_skip_epoch enabled. Will use forceSkipEpoch to skip from epoch end {} to block {} - {} '
                                    'to prevent snapshotter overload.',
                                    gap_from_head, self.GAP_THRESHOLD, contract_epoch_end, begin_block_epoch, end_block_epoch
                                )
                            else:
                                # No epoch on contract yet - use forceSkipEpoch to start from near current head
                                begin_block_epoch = cur_block - settings.chain.epoch.head_offset - self.GAP_OFFSET
                                end_block_epoch = cur_block - settings.chain.epoch.head_offset
                                use_force_skip = True
                                self._logger.warning(
                                    'Large block gap detected: {} blocks from chain head (>= threshold {}). '
                                    'force_skip_epoch enabled. No epoch on contract. Will use forceSkipEpoch to start from block {} - {}',
                                    gap_from_head, self.GAP_THRESHOLD, begin_block_epoch, end_block_epoch
                                )
                        except Exception as ex:
                            self._logger.error(
                                'Error fetching current epoch from contract: {}. Will try forceSkipEpoch.',
                                ex
                            )
                            # Fallback: try forceSkipEpoch
                            begin_block_epoch = cur_block - settings.chain.epoch.head_offset - self.GAP_OFFSET
                            end_block_epoch = cur_block - settings.chain.epoch.head_offset
                            use_force_skip = True
                        
                        # Reset polling interval for fresh start
                        self.current_polling_interval = 1.0
                        # Recalculate gaps after reset
                        block_gap = end_block_epoch - begin_block_epoch + 1
                        gap_from_head = cur_block - begin_block_epoch
                    elif gap_from_head >= self.GAP_THRESHOLD and not force_skip_enabled:
                        # Large gap detected but force_skip_epoch is disabled
                        # Sync begin_block_epoch with on-chain state for sequential release
                        # _fetch_epoch_from_contract() already returns currentEpoch.end + 1
                        try:
                            next_epoch_to_release = await self._fetch_epoch_from_contract()
                            if next_epoch_to_release != -1:
                                # next_epoch_to_release is already currentEpoch.end + 1
                                if begin_block_epoch < next_epoch_to_release:
                                    self._logger.warning(
                                        'Large block gap detected: {} blocks from chain head (>= threshold {}). '
                                        'force_skip_epoch disabled. Syncing with on-chain epoch. '
                                        'Will release sequentially from block {} to prevent snapshotter overload.',
                                        gap_from_head, self.GAP_THRESHOLD, next_epoch_to_release
                                    )
                                    begin_block_epoch = next_epoch_to_release
                        except Exception as ex:
                            self._logger.error(
                                'Error fetching current epoch from contract: {}. Using current begin_block_epoch.',
                                ex
                            )
                        
                        end_block_epoch = cur_block - settings.chain.epoch.head_offset
                        # Reset polling interval for fresh start
                        self.current_polling_interval = 1.0
                        # Recalculate gaps after reset
                        block_gap = end_block_epoch - begin_block_epoch + 1
                        gap_from_head = cur_block - begin_block_epoch
                    
                    # Check if we have enough blocks for an epoch
                    if not (end_block_epoch - begin_block_epoch + 1) >= settings.chain.epoch.height:
                        # Special handling for epoch height of 1 - process immediately when available
                        if settings.chain.epoch.height == 1:
                            # For height=1, if current_block > begin_block_epoch, we can process immediately
                            if cur_block > begin_block_epoch:
                                # Process immediately - don't wait
                                end_block_epoch = cur_block - settings.chain.epoch.head_offset
                                # Ensure we have at least 1 block
                                if end_block_epoch >= begin_block_epoch:
                                    # Will process below, skip sleep
                                    pass
                                else:
                                    # Adjust polling interval based on gap
                                    if block_gap < 5:
                                        # Small gap - use faster polling
                                        polling_interval = self.MIN_POLLING_INTERVAL
                                    else:
                                        # Larger gap but below threshold - use adaptive interval
                                        polling_interval = self.current_polling_interval
                                    
                                    self._logger.debug(
                                        'Current head {} after offsetting | '
                                        'Begin block {} - End block {} does not satisfy epoch length (height=1). '
                                        'Using adaptive polling, sleeping for {:.2f} seconds...',
                                        end_block_epoch, begin_block_epoch, end_block_epoch, polling_interval
                                    )
                                    await asyncio.sleep(polling_interval)
                                    continue
                            else:
                                # No new blocks yet - use adaptive polling interval
                                polling_interval = self.current_polling_interval
                                self._logger.debug(
                                    'No new blocks available. Using adaptive polling, sleeping for {:.2f} seconds...',
                                    polling_interval
                                )
                                await asyncio.sleep(polling_interval)
                                continue
                        else:
                            # Original logic for epoch height > 1
                            sleep_factor = settings.chain.epoch.height - \
                                ((end_block_epoch - begin_block_epoch) + 1)
                            self._logger.debug(
                                'Current head of source chain estimated at block {} after offsetting | '
                                '{} - {} does not satisfy configured epoch length. '
                                'Sleeping for {} seconds for {} blocks to accumulate....',
                                end_block_epoch, begin_block_epoch, end_block_epoch,
                                sleep_factor * settings.chain.epoch.block_time, sleep_factor,
                            )
                            await asyncio.sleep(
                                sleep_factor *
                                settings.chain.epoch.block_time,
                            )
                        continue
                    self._logger.debug(
                        'Chunking blocks between {} - {} with chunk size: {}', begin_block_epoch,
                        end_block_epoch, settings.chain.epoch.height,
                    )
                    
                    epochs_processed = 0
                    # use_force_skip is set above when gap >= threshold
                    catching_up = gap_from_head > 0
                    
                    # CRITICAL: Sync begin_block_epoch to contract's next epoch ONCE before processing
                    # This ensures we start from the correct position, then continue sequentially
                    if not use_force_skip:
                        try:
                            contract_next_epoch = await self._fetch_epoch_from_contract()
                            if contract_next_epoch != -1 and begin_block_epoch < contract_next_epoch:
                                self._logger.info(
                                    'Syncing begin_block_epoch to contract\'s next epoch: {} -> {}',
                                    begin_block_epoch, contract_next_epoch
                                )
                                begin_block_epoch = contract_next_epoch
                        except Exception as sync_error:
                            self._logger.warning(
                                'Error fetching contract epoch for sync: {}. Continuing with current begin_block_epoch.',
                                sync_error
                            )
                    
                    # Process epochs sequentially from begin_block_epoch
                    for epoch in chunks(begin_block_epoch, end_block_epoch, settings.chain.epoch.height):
                        if epoch[1] - epoch[0] + 1 < settings.chain.epoch.height:
                            self._logger.debug(
                                'Skipping chunk of blocks {} - {} as minimum epoch size not satisfied | '
                                'Resetting chunking to begin from block {}',
                                epoch[0], epoch[1], epoch[0],
                            )
                            begin_block_epoch = epoch[0]
                            break
                        epoch_block = {'begin': epoch[0], 'end': epoch[1]}
                        self._logger.debug(
                            'Epoch of sufficient length found: {}', epoch_block,
                        )

                        try:
                            # Determine release epoch and function to use
                            # Simple logic: use forceSkipEpoch only for intentional jumps, otherwise releaseEpoch
                            if use_force_skip:
                                # Intentional jump to head - use forceSkipEpoch
                                release_epoch = epoch_block.copy()
                                function_name = 'forceSkipEpoch'
                            else:
                                # Sequential release - always use releaseEpoch
                                release_epoch = epoch_block.copy()
                                function_name = 'releaseEpoch'
                            
                            self._logger.info(
                                'Attempting to {} epoch - {}',
                                function_name, release_epoch
                            )
                            
                            # Always check receipts for reliability - ensures we detect failures immediately
                            # and maintain nonce consistency. Removed the non-receipt path which caused
                            # silent failures and nonce drift issues.
                            self.release_counter += 1
                            
                            # Identity: forceSkipEpoch requires DataMarket owner (force_consensus); releaseEpoch requires epochManager (validator).
                            # Only use force_consensus identity when function_name is forceSkipEpoch (intentional jump).
                            if function_name == 'forceSkipEpoch':
                                _address = settings.force_consensus_address
                                _key = settings.force_consensus_private_key
                                # Always fetch nonce from chain right before sending to avoid nonce drift
                                _nonce = await w3.eth.get_transaction_count(_address)
                            else:
                                _address = settings.validator_epoch_address
                                _key = settings.validator_epoch_private_key
                                # Always fetch nonce from chain right before sending to avoid nonce drift
                                _nonce = await w3.eth.get_transaction_count(_address)
                            
                            # Submit transaction (fire-and-forget pattern like relayer)
                            # Don't block on receipt - epoch release should continue immediately
                            tx_hash = await write_transaction(
                                w3,
                                _address,
                                _key,
                                protocol_state_contract,
                                function_name,
                                _nonce,
                                self.gas if not self._force_tx else self.high_gas,
                                Web3.to_checksum_address(
                                    data_market_address,
                                ),
                                release_epoch['begin'],
                                release_epoch['end'],
                            )
                            
                            self._logger.info(
                                '✅ Epoch Release Transaction Submitted! TX: {} | Nonce: {}',
                                tx_hash.hex() if hasattr(tx_hash, 'hex') else tx_hash,
                                _nonce
                            )
                            
                            # Wait for receipt in background for nonce management (non-blocking)
                            # Following relayer pattern: receipt waiting happens separately
                            # Nonce will be refreshed from chain before next transaction
                            receipt = None
                            try:
                                # Try to get receipt quickly (with short timeout)
                                # If it's not available yet, that's fine - we'll refresh nonce from chain next time
                                receipt = await asyncio.wait_for(
                                    w3.eth.wait_for_transaction_receipt(tx_hash, timeout=10.0),
                                    timeout=10.0
                                )
                                if receipt and receipt.get('status') == 1:
                                    self._logger.info(
                                        'Transaction {} confirmed successfully (status=1)',
                                        tx_hash.hex() if hasattr(tx_hash, 'hex') else tx_hash
                                    )
                                elif receipt and receipt.get('status') == 0:
                                    self._logger.warning(
                                        'Transaction {} reverted on-chain (status=0)',
                                        tx_hash.hex() if hasattr(tx_hash, 'hex') else tx_hash
                                    )
                            except (asyncio.TimeoutError, Exception) as receipt_error:
                                # Receipt not available yet - that's fine, transaction was submitted
                                # Nonce will be refreshed from chain before next transaction
                                self._logger.debug(
                                    'Receipt not available yet for TX {} (will refresh nonce from chain): {}',
                                    tx_hash.hex() if hasattr(tx_hash, 'hex') else tx_hash,
                                    receipt_error
                                )
                            
                            # Handle transaction result - simplified logic flow
                            # Case 1: Receipt exists and transaction failed (status != 1)
                            if receipt and receipt.get('status') != 1:
                                # E22 (epoch already exists) - common case, sync and break to recalculate
                                if function_name != 'forceSkipEpoch':
                                    self._logger.warning(
                                        'Transaction failed (may be E22 - epoch already exists). '
                                        'Syncing and recalculating.',
                                        release_epoch['begin']
                                    )
                                    # Refresh nonces and sync to contract's next epoch
                                    self._nonce = await w3.eth.get_transaction_count(
                                        settings.validator_epoch_address,
                                    )
                                    self._force_skip_nonce = await w3.eth.get_transaction_count(
                                        settings.force_consensus_address,
                                    )
                                    next_epoch = await self._fetch_epoch_from_contract()
                                    if next_epoch != -1:
                                        begin_block_epoch = next_epoch
                                    # Break to recalculate chunks from updated begin_block_epoch
                                    break
                                
                                # forceSkipEpoch failed - try fallback to releaseEpoch
                                if function_name == 'forceSkipEpoch':
                                    self._logger.error(
                                        'forceSkipEpoch failed (likely permission issue). '
                                        'Falling back to sequential releaseEpoch. Receipt: {}', receipt,
                                    )
                                    try:
                                        next_epoch_to_release = await self._fetch_epoch_from_contract()
                                        if next_epoch_to_release != -1:
                                            retry_nonce = await w3.eth.get_transaction_count(
                                                settings.validator_epoch_address,
                                            )
                                            retry_tx_hash = await write_transaction(
                                                w3,
                                                settings.validator_epoch_address,
                                                settings.validator_epoch_private_key,
                                                protocol_state_contract,
                                                'releaseEpoch',
                                                retry_nonce,
                                                self.gas if not self._force_tx else self.high_gas,
                                                Web3.to_checksum_address(data_market_address),
                                                next_epoch_to_release,
                                                next_epoch_to_release,
                                            )
                                            self._logger.info(
                                                'Retry transaction submitted: TX {} | Nonce: {}',
                                                retry_tx_hash.hex() if hasattr(retry_tx_hash, 'hex') else retry_tx_hash,
                                                retry_nonce
                                            )
                                            # Try to get receipt (non-blocking)
                                            retry_receipt = None
                                            try:
                                                retry_receipt = await asyncio.wait_for(
                                                    w3.eth.wait_for_transaction_receipt(retry_tx_hash, timeout=10.0),
                                                    timeout=10.0
                                                )
                                            except (asyncio.TimeoutError, Exception):
                                                pass
                                            
                                            if retry_receipt and retry_receipt.get('status') == 1:
                                                # Fallback succeeded - update and continue sequentially
                                                self._logger.info(
                                                    'Successfully released epoch {} using releaseEpoch after forceSkipEpoch failed',
                                                    next_epoch_to_release
                                                )
                                                self._nonce = await w3.eth.get_transaction_count(
                                                    settings.validator_epoch_address,
                                                )
                                                epochs_processed += 1
                                                self._force_tx = False
                                                begin_block_epoch = next_epoch_to_release + 1
                                                self._logger.debug(
                                                    'Continuing sequentially from {} after fallback success',
                                                    begin_block_epoch
                                                )
                                                break  # Break for loop to recalculate end_block_epoch
                                    except Exception as fallback_ex:
                                        self._logger.error(
                                            'Error during fallback to releaseEpoch: {}', fallback_ex
                                        )
                                
                                # Transaction failed - create issue and handle
                                tx_hash_str = receipt.get('transactionHash', 'Unknown')
                                if hasattr(tx_hash_str, 'hex'):
                                    tx_hash_str = tx_hash_str.hex()
                                receipt_json = Web3.to_json(receipt)
                                self._logger.error(
                                    'Unable to release epoch, txn failed! TX: {}, Receipt: {}',
                                    tx_hash_str, receipt_json,
                                )
                                issue = GenericTxnIssue(
                                    accountAddress=_address,
                                    epochBegin=str(release_epoch['begin']),
                                    issueType='EpochReleaseTxnFailed',
                                    extra=f"Transaction Hash: {tx_hash_str}\nReceipt: {receipt_json}",
                                )
                                
                                # Handle failure: notify, wait, sync, break
                                await send_failure_notifications(client=self._client, message=issue)
                                time.sleep(30)
                                self._nonce = await w3.eth.get_transaction_count(
                                    settings.validator_epoch_address,
                                )
                                self._force_skip_nonce = await w3.eth.get_transaction_count(
                                    settings.force_consensus_address,
                                )
                                next_epoch = await self._fetch_epoch_from_contract()
                                if next_epoch != -1:
                                    begin_block_epoch = next_epoch
                                self._force_tx = True
                                break  # Break for loop
                            
                            # Case 2: Receipt is None (timeout) OR receipt.status == 1 (success)
                            else:
                                # Transaction submitted, but receipt may be None if timeout occurred
                                if receipt is None:
                                    # Receipt timeout - verify contract state to check if transaction was included
                                    self._logger.debug(
                                        'Receipt not available for TX {} (timeout). Verifying contract state to check if transaction was included.',
                                        tx_hash.hex() if hasattr(tx_hash, 'hex') else tx_hash
                                    )
                                    try:
                                        contract_next_epoch = await self._fetch_epoch_from_contract()
                                        if contract_next_epoch != -1:
                                            # Check if transaction was included by comparing contract state
                                            # _fetch_epoch_from_contract() returns currentEpoch.end + 1 (next epoch to release)
                                            # If we tried to release epoch N and transaction was included:
                                            #   - Contract's current epoch end becomes N
                                            #   - contract_next_epoch = N + 1
                                            #   - contract_next_epoch > release_epoch['begin'] (N+1 > N) → TRUE
                                            # If transaction was NOT included:
                                            #   - Contract's current epoch end remains < N
                                            #   - contract_next_epoch <= release_epoch['begin']
                                            if contract_next_epoch > release_epoch['begin']:
                                                # Transaction was included! Contract advanced past what we tried to release
                                                self._logger.info(
                                                    'Receipt timeout but transaction was included. Contract epoch {} > release epoch {}. '
                                                    'Updating begin_block_epoch from {} to {}',
                                                    contract_next_epoch, release_epoch['begin'], begin_block_epoch, contract_next_epoch
                                                )
                                                begin_block_epoch = contract_next_epoch
                                                epochs_processed += 1
                                                self._force_tx = False
                                                # Refresh nonces
                                                if function_name == 'forceSkipEpoch':
                                                    self._force_skip_nonce = await w3.eth.get_transaction_count(
                                                        settings.force_consensus_address,
                                                    )
                                                else:
                                                    self._nonce = await w3.eth.get_transaction_count(
                                                        settings.validator_epoch_address,
                                                    )
                                                break
                                            else:
                                                # Transaction not included - contract_next_epoch <= release_epoch['begin']
                                                # Keep begin_block_epoch unchanged, will retry
                                                self._logger.debug(
                                                    'Receipt timeout and transaction not included. Contract epoch {} <= release epoch {}. '
                                                    'Keeping begin_block_epoch {} for retry',
                                                    contract_next_epoch, release_epoch['begin'], begin_block_epoch
                                                )
                                                # Refresh nonces but don't update begin_block_epoch
                                                if function_name == 'forceSkipEpoch':
                                                    self._force_skip_nonce = await w3.eth.get_transaction_count(
                                                        settings.force_consensus_address,
                                                    )
                                                else:
                                                    self._nonce = await w3.eth.get_transaction_count(
                                                        settings.validator_epoch_address,
                                                    )
                                                break
                                    except Exception as verify_error:
                                        self._logger.error(
                                            'Error verifying contract state after receipt timeout: {}. Keeping current begin_block_epoch.',
                                            verify_error
                                        )
                                        # Refresh nonces but don't update begin_block_epoch
                                        if function_name == 'forceSkipEpoch':
                                            self._force_skip_nonce = await w3.eth.get_transaction_count(
                                                settings.force_consensus_address,
                                            )
                                        else:
                                            self._nonce = await w3.eth.get_transaction_count(
                                                settings.validator_epoch_address,
                                            )
                                        break
                                else:
                                    # Receipt exists and status == 1 (confirmed success)
                                    self._force_tx = False
                                    
                                    # Refresh nonces from chain
                                    if function_name == 'forceSkipEpoch':
                                        self._force_skip_nonce = await w3.eth.get_transaction_count(
                                            settings.force_consensus_address,
                                        )
                                    else:
                                        self._nonce = await w3.eth.get_transaction_count(
                                            settings.validator_epoch_address,
                                        )
                                    epochs_processed += 1
                                    
                                    # CRITICAL: Update begin_block_epoch to continue sequentially
                                    # This ensures we catch up by releasing epochs sequentially
                                    begin_block_epoch = release_epoch['end'] + 1
                                    self._logger.debug(
                                        'Successfully released epoch {}-{}, continuing sequentially from {}',
                                        release_epoch['begin'], release_epoch['end'], begin_block_epoch
                                    )
                                    # Break out of for loop to recalculate end_block_epoch from chain head
                                    # This allows processing multiple epochs per outer loop iteration
                                    break
                        except Exception as ex:
                            # Check if this is a "nonce too low" error - means transaction was already included
                            error_str = str(ex)
                            is_nonce_error = 'nonce too low' in error_str.lower()
                            
                            if is_nonce_error:
                                # Nonce error means a previous transaction was included
                                # Refresh nonces from chain immediately
                                self._logger.warning(
                                    'Nonce too low error detected - previous transaction was likely included. '
                                    'Refreshing nonces from chain. Error: {}', ex,
                                )
                                # Determine which identity was used based on function_name
                                if use_force_skip or function_name == 'forceSkipEpoch':
                                    self._force_skip_nonce = await w3.eth.get_transaction_count(
                                        settings.force_consensus_address,
                                    )
                                else:
                                    self._nonce = await w3.eth.get_transaction_count(
                                        settings.validator_epoch_address,
                                    )
                                # Sync epoch and break to recalculate
                                next_epoch = await self._fetch_epoch_from_contract()
                                if next_epoch != -1:
                                    self._logger.info(
                                        'Syncing begin_block_epoch after nonce error: {} -> {}',
                                        begin_block_epoch, next_epoch
                                    )
                                    begin_block_epoch = next_epoch
                                # Break out of for loop to recalculate end_block_epoch from chain head
                                break
                            
                            # Check if this is a timeout error - transaction might still be included
                            is_timeout_error = isinstance(ex, asyncio.TimeoutError) or 'timeout' in error_str.lower()
                            if is_timeout_error:
                                self._logger.warning(
                                    'Transaction submission error (timeout/RPC issue). Verifying contract state to check if transaction was included. Error: {}', ex,
                                )
                                # Refresh nonces
                                if use_force_skip or function_name == 'forceSkipEpoch':
                                    self._force_skip_nonce = await w3.eth.get_transaction_count(
                                        settings.force_consensus_address,
                                    )
                                else:
                                    self._nonce = await w3.eth.get_transaction_count(
                                        settings.validator_epoch_address,
                                    )
                                
                                # CRITICAL: Verify contract state after timeout
                                # Transaction may have been included even though receipt timed out
                                try:
                                    contract_next_epoch = await self._fetch_epoch_from_contract()
                                    if contract_next_epoch != -1:
                                        # Check if transaction was included by comparing contract state
                                        # _fetch_epoch_from_contract() returns currentEpoch.end + 1 (next epoch to release)
                                        # If we tried to release epoch N and transaction was included:
                                        #   - Contract's current epoch end becomes N
                                        #   - contract_next_epoch = N + 1
                                        #   - contract_next_epoch > release_epoch['begin'] (N+1 > N) → TRUE
                                        # If transaction was NOT included:
                                        #   - Contract's current epoch end remains < N
                                        #   - contract_next_epoch <= release_epoch['begin']
                                        if contract_next_epoch > release_epoch['begin']:
                                            # Transaction was included! Contract advanced past what we tried to release
                                            self._logger.info(
                                                'Timeout occurred but transaction was included. Contract epoch {} > release epoch {}. '
                                                'Updating begin_block_epoch from {} to {}',
                                                contract_next_epoch, release_epoch['begin'], begin_block_epoch, contract_next_epoch
                                            )
                                            begin_block_epoch = contract_next_epoch
                                        else:
                                            # Transaction not included - contract_next_epoch <= release_epoch['begin']
                                            # Keep begin_block_epoch unchanged, will retry
                                            self._logger.debug(
                                                'Timeout occurred and transaction not included. Contract epoch {} <= release epoch {}. '
                                                'Keeping begin_block_epoch {} for retry',
                                                contract_next_epoch, release_epoch['begin'], begin_block_epoch
                                            )
                                except Exception as verify_error:
                                    self._logger.error(
                                        'Error verifying contract state after timeout: {}. Keeping current begin_block_epoch.',
                                        verify_error
                                    )
                                
                                # Break to retry in next outer loop iteration
                                break
                            
                            # Log full exception details with traceback for other errors
                            self._logger.opt(exception=True).error(
                                'Unable to release epoch, error: {}', ex,
                            )

                            # Format exception details for issue reporting
                            exception_details = ''.join(traceback.format_exception(type(ex), ex, ex.__traceback__))
                            
                            issue = GenericTxnIssue(
                                accountAddress=settings.validator_epoch_address,
                                epochBegin=str(epoch_block['begin']),
                                issueType='EpochReleaseError',
                                extra=f"Exception: {str(ex)}\nTraceback:\n{exception_details}",
                            )

                            await send_failure_notifications(client=self._client, message=issue)

                            # sleep for 30 seconds to avoid nonce collision
                            time.sleep(30)
                            # reset nonces for both identities
                            self._nonce = await w3.eth.get_transaction_count(
                                settings.validator_epoch_address,
                            )
                            self._force_skip_nonce = await w3.eth.get_transaction_count(
                                settings.force_consensus_address,
                            )

                            # Fetch epoch again to sync with contract state
                            next_epoch = await self._fetch_epoch_from_contract()
                            if next_epoch != -1:
                                self._logger.info(
                                    'Syncing begin_block_epoch to contract epoch after error: {} -> {}',
                                    begin_block_epoch, next_epoch
                                )
                                begin_block_epoch = next_epoch

                            self._force_tx = True
                            break  # Break for loop to retry in next outer loop iteration

                        # Skip sleep when catching up (any gap > 0) to catch up faster
                        # Only sleep when we're caught up (gap == 0)
                        # Recalculate gap to see if we're still catching up (cur_block stays same, epoch[1] is last processed)
                        current_gap = cur_block - epoch[1]  # Gap from chain head to last processed epoch
                        if current_gap > 0:
                            # When catching up (any gap > 0), use minimal sleep (0.1s) to process epochs as fast as possible
                            # This allows us to catch up quickly without overwhelming the chain
                            self._logger.debug(
                                'Catching up (gap: {} blocks). Using minimal sleep (0.1s) to process epochs faster.',
                                current_gap
                            )
                            await asyncio.sleep(0.1)  # Minimal sleep to allow async operations
                        else:
                            # Only sleep when caught up (gap == 0)
                            self._logger.debug(
                                'Caught up (gap: {} blocks). Waiting to push next epoch in {} seconds...',
                                current_gap, sleep_secs_between_chunks
                            )
                            await asyncio.sleep(sleep_secs_between_chunks)
                    else:
                        begin_block_epoch = end_block_epoch + 1
                        
                        # Performance tracking and adaptive polling adjustment
                        processing_time = time.time() - processing_start_time
                        if epochs_processed > 0:
                            await self._adaptive_polling_adjustment(epochs_processed, processing_time)
                        
                        # Adjust polling interval based on remaining gap
                        remaining_gap = cur_block - begin_block_epoch
                        if remaining_gap < 5:
                            # Small gap - use faster polling to catch up quickly
                            self.current_polling_interval = max(
                                self.current_polling_interval * 0.9,
                                self.MIN_POLLING_INTERVAL
                            )
                        elif remaining_gap == 0:
                            # Caught up - use slower polling to reduce RPC calls
                            self.current_polling_interval = min(
                                self.current_polling_interval * 1.1,
                                self.MAX_POLLING_INTERVAL
                            )


def main():
    """Spin up the ticker process in event loop"""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(
        resource.RLIMIT_NOFILE,
        (settings.rlimit.file_descriptors, hard),
    )
    loop = uvloop.new_event_loop()
    asyncio.set_event_loop(loop)

    ticker_process = EpochGenerator()
    loop.run_until_complete(ticker_process.run())


if __name__ == '__main__':
    main()
