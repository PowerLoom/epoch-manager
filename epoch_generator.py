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
                    # Track if we're catching up (any gap > 0 means we're behind and should process faster
                    catching_up = gap_from_head > 0
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
                            # Fetch current epoch from contract to determine what to release
                            epoch_data = await protocol_state_contract.functions.currentEpoch(
                                Web3.to_checksum_address(data_market_address)
                            ).call()
                            epoch_end = epoch_data[1] if epoch_data[1] else None
                            next_epoch = epoch_end + 1 if epoch_end is not None else None
                            
                            # If we have a next epoch and it differs from epoch_block, sync to it
                            # Exception: when use_force_skip is True we are intentionally jumping to head;
                            # do NOT override with contract's next sequential epoch - use the jump target (epoch_block).
                            if use_force_skip:
                                release_epoch = epoch_block.copy()
                            elif next_epoch is not None and epoch_block['begin'] != next_epoch:
                                self._logger.info(
                                    'Syncing to epoch {} (contract next: {}, calculated: {})',
                                    next_epoch, next_epoch, epoch_block['begin']
                                )
                                release_epoch = {'begin': next_epoch, 'end': next_epoch}
                            else:
                                # Contract is in sync, use calculated epoch_block
                                release_epoch = epoch_block.copy()
                            
                            # Primary: use_force_skip means we decided at top to jump to head → must use forceSkipEpoch (non-sequential; owner-only).
                            # Secondary: when not in a jump, use forceSkipEpoch only if gap from head to release target is >= threshold.
                            if use_force_skip:
                                function_name = 'forceSkipEpoch'
                            else:
                                gap_to_release = cur_block - release_epoch['end']
                                function_name = (
                                    'forceSkipEpoch'
                                    if (gap_to_release >= self.GAP_THRESHOLD and force_skip_enabled)
                                    else 'releaseEpoch'
                                )
                            
                            self._logger.info(
                                'Attempting to {} epoch - {}',
                                function_name, release_epoch
                            )
                            
                            # Always check receipts for reliability - ensures we detect failures immediately
                            # and maintain nonce consistency. Removed the non-receipt path which caused
                            # silent failures and nonce drift issues.
                            self.release_counter += 1
                            
                            # Identity: forceSkipEpoch requires DataMarket owner (force_consensus); releaseEpoch requires epochManager (validator).
                            # Primary: when use_force_skip we are doing a jump → we call forceSkipEpoch → must use owner identity.
                            # Secondary: when not use_force_skip but function_name is forceSkipEpoch (gap_to_release large), same.
                            if use_force_skip or function_name == 'forceSkipEpoch':
                                _address, _key, _nonce = (
                                    settings.force_consensus_address,
                                    settings.force_consensus_private_key,
                                    self._force_skip_nonce,
                                )
                            else:
                                _address, _key, _nonce = (
                                    settings.validator_epoch_address,
                                    settings.validator_epoch_private_key,
                                    self._nonce,
                                )
                            tx_hash, receipt = await write_transaction_with_receipt(
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

                            # Check transaction receipt
                            if receipt['status'] != 1:
                                # E22 (epoch already exists) can happen due to redundant submissions
                                # from multiple epoch managers or duplicate transactions - just sync and continue
                                if function_name != 'forceSkipEpoch':
                                    self._logger.warning(
                                        'Transaction failed (may be E22 - epoch already exists). '
                                        'Syncing and continuing.',
                                        epoch_block['begin']
                                    )
                                    # Tx may have been mined; refresh nonces so next attempt does not get "nonce too low"
                                    self._nonce = await w3.eth.get_transaction_count(
                                        settings.validator_epoch_address,
                                    )
                                    self._force_skip_nonce = await w3.eth.get_transaction_count(
                                        settings.force_consensus_address,
                                    )
                                    next_epoch = await self._fetch_epoch_from_contract()
                                    if next_epoch != -1:
                                        begin_block_epoch = next_epoch
                                    # Continue to next iteration
                                    continue
                                
                                # Check if forceSkipEpoch failed due to permission issues
                                if function_name == 'forceSkipEpoch':
                                    self._logger.error(
                                        'forceSkipEpoch failed (likely permission issue - requires owner). '
                                        'Falling back to sequential releaseEpoch. Receipt: {}', receipt,
                                    )
                                    # Fall back to sequential releaseEpoch
                                    function_name = 'releaseEpoch'
                                    # Sync with on-chain epoch for sequential release
                                    # _fetch_epoch_from_contract() already returns currentEpoch.end + 1
                                    try:
                                        next_epoch_to_release = await self._fetch_epoch_from_contract()
                                        if next_epoch_to_release != -1:
                                            # next_epoch_to_release is already currentEpoch.end + 1
                                            if epoch_block['begin'] != next_epoch_to_release:
                                                self._logger.warning(
                                                    'Adjusting epoch to sequential: {} -> {}',
                                                    epoch_block['begin'], next_epoch_to_release
                                                )
                                                epoch_block['begin'] = next_epoch_to_release
                                                epoch_block['end'] = next_epoch_to_release
                                                # Retry with releaseEpoch
                                                tx_hash, receipt = await write_transaction_with_receipt(
                                                    w3,
                                                    settings.validator_epoch_address,
                                                    settings.validator_epoch_private_key,
                                                    protocol_state_contract,
                                                    'releaseEpoch',
                                                    self._nonce,
                                                    self.gas if not self._force_tx else self.high_gas,
                                                    Web3.to_checksum_address(
                                                        data_market_address,
                                                    ),
                                                    epoch_block['begin'],
                                                    epoch_block['end'],
                                                )
                                                if receipt['status'] == 1:
                                                    # Success with releaseEpoch, continue normally
                                                    self._logger.info(
                                                        'Successfully released epoch {} using releaseEpoch after forceSkipEpoch failed',
                                                        epoch_block
                                                    )
                                                    # Update nonce and continue
                                                    self._nonce += 1
                                                    epochs_processed += 1
                                                    self._force_tx = False
                                                    # Skip the error handling below since we succeeded
                                                    continue
                                                else:
                                                    # Still failed, log error
                                                    self._logger.error(
                                                        'releaseEpoch also failed after forceSkipEpoch. Receipt: {}', receipt
                                                    )
                                    except Exception as fallback_ex:
                                        self._logger.error(
                                            'Error during fallback to releaseEpoch: {}', fallback_ex
                                        )
                                
                                if receipt['status'] != 1:
                                    # Extract transaction hash and error details from receipt
                                    tx_hash_str = receipt.get('transactionHash', 'Unknown')
                                    if hasattr(tx_hash_str, 'hex'):
                                        tx_hash_str = tx_hash_str.hex()
                                    
                                    receipt_json = Web3.to_json(receipt)
                                    self._logger.error(
                                        'Unable to release epoch, txn failed! TX: {}, Receipt: {}',
                                        tx_hash_str, receipt_json,
                                    )
                                    issue = GenericTxnIssue(
                                        accountAddress=settings.validator_epoch_address,
                                        epochBegin=str(epoch_block['begin']),
                                        issueType='EpochReleaseTxnFailed',
                                        extra=f"Transaction Hash: {tx_hash_str}\nReceipt: {receipt_json}",
                                    )
                            else:
                                issue = None
                                # Log successful release
                                if receipt and receipt.get('status') == 1:
                                    self._logger.info(
                                        '✅ Epoch Released! TX: {}',
                                        tx_hash.hex() if hasattr(tx_hash, 'hex') else tx_hash
                                    )

                            if issue:
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

                                # Sync with contract
                                next_epoch = await self._fetch_epoch_from_contract()
                                if next_epoch != -1:
                                    self._logger.info(
                                        'Syncing begin_block_epoch to contract epoch: {} -> {}',
                                        begin_block_epoch, next_epoch
                                    )
                                    begin_block_epoch = next_epoch
                                self._force_tx = True
                                break
                            else:
                                self._force_tx = False
                                # Success - increment the nonce we used (force_consensus for forceSkipEpoch, validator for releaseEpoch)
                                if function_name == 'forceSkipEpoch':
                                    self._force_skip_nonce += 1
                                else:
                                    self._nonce += 1
                                epochs_processed += 1
                        except Exception as ex:
                            # Log full exception details with traceback
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
                            break

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
