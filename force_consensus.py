import asyncio
import json
import random
import threading
import time
from collections import defaultdict
import resource

import aiorwlock
import uvloop
from httpx import AsyncClient
from httpx import AsyncHTTPTransport
from httpx import Limits
from httpx import Timeout
from redis import asyncio as aioredis
from web3 import AsyncHTTPProvider
from web3 import AsyncWeb3

from tenacity import retry
from tenacity import retry_if_exception_type
from tenacity import stop_after_attempt
from tenacity import wait_random_exponential

from data_models import GenericTxnIssue
from helpers.redis_keys import event_detector_last_processed_block
from rpc import get_event_sig_and_abi
from rpc import RpcHelper
from settings.conf import settings
from utils.default_logger import logger
from utils.helpers import semaphore_then_aiorwlock_aqcuire_release
from utils.notification_utils import send_failure_notifications
from utils.redis_conn import RedisPool
from utils.transaction_utils import write_transaction
protocol_state_contract_address = settings.protocol_state_address

# load abi from json file and create contract object
with open('utils/static/abi.json', 'r') as f:
    abi = json.load(f)
w3 = AsyncWeb3(AsyncHTTPProvider(settings.anchor_chain.rpc.full_nodes[0].url))

protocol_state_contract = w3.eth.contract(
    address=protocol_state_contract_address, abi=abi,
)


class ForceConsensus:
    _aioredis_pool: RedisPool
    _reader_redis_pool: aioredis.Redis
    _writer_redis_pool: aioredis.Redis

    def __init__(self, name='ForceConsensus'):
        self._logger = logger.bind(module=name)
        self._shutdown_initiated = False
        self.last_sent_block = 0
        self._end = None
        self._rwlock = None
        self._epochId = 1
        self._pending_epochs = set()
        self._submission_window = 0
        self._semaphore = asyncio.Semaphore(value=20)
        self._nonce = -1
        self.rpc_helper = RpcHelper(rpc_settings=settings.anchor_chain.rpc)
        self._last_processed_block = 0
        self._client = None
        self._async_transport = None
        self._projects_submitted_for_epoch = defaultdict(set)
        self._finalized_epochs = defaultdict(set)
        self.gas = settings.anchor_chain.default_gas_in_gwei

        EVENTS_ABI = {
            'EpochReleased': protocol_state_contract.events.EpochReleased._get_event_abi(),
            'SnapshotFinalized': protocol_state_contract.events.SnapshotFinalized._get_event_abi(),
            'SnapshotSubmitted': protocol_state_contract.events.SnapshotSubmitted._get_event_abi(),
        }


        EVENT_SIGS = {
            'EpochReleased': 'EpochReleased(uint256,uint256,uint256,uint256)',
            'SnapshotFinalized': 'SnapshotFinalized(uint256,uint256,string,string,uint256,uint256,uint256)',
            'SnapshotSubmitted': 'SnapshotSubmitted(address,uint256,string,uint256,string,uint256)',
        }

        self.event_sig, self.event_abi = get_event_sig_and_abi(
            EVENT_SIGS,
            EVENTS_ABI,
        )

    async def get_events(self, from_block: int, to_block: int):
        """Get the events from the block range.

        Arguments:
            int : from block
            int: to block

        Returns:
            list : (type, event)
        """
        events_log = await self.rpc_helper.get_events_logs(
            **{
                'contract_address': protocol_state_contract_address,
                'to_block': to_block,
                'from_block': from_block,
                'topics': [self.event_sig],
                'event_abi': self.event_abi,
                'redis_conn': self._writer_redis_pool,
            },
        )
        for log in events_log:
            if log['event'] == 'EpochReleased':
                self._pending_epochs.add((time.time(), log['args']['epochId']))
                self._logger.info(
                    'Epoch release detected, adding epoch: {} to pending epochs', log['args']['epochId'],
                )
            elif log['event'] == 'SnapshotFinalized':
                self._finalized_epochs[log['args']['epochId']].add(log['args']['projectId'])
            elif log['event'] == 'SnapshotSubmitted':
                self._projects_submitted_for_epoch[log['args']['epochId']].add(log['args']['projectId'])

        asyncio.ensure_future(self._force_complete_consensus())

    async def setup(self):
        self._aioredis_pool = RedisPool(writer_redis_conf=settings.redis)
        self._nonce = await w3.eth.get_transaction_count(
            settings.validator_epoch_address,
        )

        await self._aioredis_pool.populate()
        self._reader_redis_pool = self._aioredis_pool.reader_redis_pool
        self._writer_redis_pool = self._aioredis_pool.writer_redis_pool
        self.redis_thread: threading.Thread

        if not self._rwlock:
            self._rwlock = aiorwlock.RWLock()

        self._nonce = await w3.eth.get_transaction_count(
            settings.force_consensus_address,
        )
        await self._init_httpx_client()

        self._submission_window = await protocol_state_contract.functions.snapshotSubmissionWindow().call()

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

    @retry(
        reraise=True,
        retry=retry_if_exception_type(Exception),
        wait=wait_random_exponential(multiplier=1, max=10),
        stop=stop_after_attempt(settings.anchor_chain.rpc.retry),
    )
    @semaphore_then_aiorwlock_aqcuire_release
    async def _make_transaction(self, project, epochId):
        tx_hash = await write_transaction(
            w3,
            settings.force_consensus_address,
            settings.force_consensus_private_key,
            protocol_state_contract,
            'forceCompleteConsensusSnapshot',
            self._nonce,
            self.gas,
            project,
            epochId,
        )
        self._nonce += 1
        return tx_hash

    @semaphore_then_aiorwlock_aqcuire_release
    async def _reset_nonce(self):
        self._logger.info('Resetting nonce')
        # sleep for 30 seconds to avoid nonce collision
        time.sleep(30)

        self._nonce = await w3.eth.get_transaction_count(
                settings.force_consensus_address,
        )

    async def _call_force_complete_consensus(self, project, epochId):
        try:
            rand = random.choice(range(0, 100))
            # check receipt and heal if needed 1% of the time
            tx_hash = await self._make_transaction(project, epochId)

            if rand == 1:
                receipt = await w3.eth.wait_for_transaction_receipt(tx_hash)

                if not receipt or receipt['status'] != 1:
                    self._logger.error(
                        'Unable to force complete consensus for project: {}, epoch: {}, txhash: {}',
                        project,
                        epochId,
                        tx_hash,
                    )

                    issue = GenericTxnIssue(
                        accountAddress=settings.force_consensus_address,
                        epochId=epochId,
                        issueType='ForceConsensusTxnFailed',
                        projectId=project,
                        extra=json.dumps(receipt),
                    )

                    await send_failure_notifications(
                        client=self._client,
                        message=issue,
                    )

                    await self._reset_nonce()
                    raise Exception('Transaction failed!')

            self._logger.info(
                'Force completing consensus for project: {}, epoch: {}, txhash: {}', project, epochId, tx_hash,
            )
        except Exception as ex:
            self._logger.error(
                'Unable to force complete consensus for project: {}, error: {}', project, ex,
            )

            issue = GenericTxnIssue(
                accountAddress=settings.force_consensus_address,
                epochId=epochId,
                issueType='ForceConsensusTxnFailed',
                projectId=project,
                extra=str(ex),
            )

            await send_failure_notifications(
                client=self._client,
                message=issue,
            )

            await self._reset_nonce()

    async def _force_complete_consensus(self):
        epochs_to_process = []
        epochs_to_remove = set()
        for release_time, epoch in self._pending_epochs:
            # anchor chain block time + 30 seconds buffer
            if release_time + (self._submission_window * settings.anchor_chain.block_time) + 30 < time.time():
                epochs_to_process.append(epoch)
                epochs_to_remove.add((release_time, epoch))

        self._pending_epochs -= epochs_to_remove

        self._logger.info('Processing Epochs {}', epochs_to_process)
        if epochs_to_process:

            txn_tasks = []
            for epochId in epochs_to_process:
                projects_to_process = self._projects_submitted_for_epoch[epochId] - self._finalized_epochs[epochId]
                self._logger.info(
                    'Force completing consensus for {} projects', len(projects_to_process),
                )

                for project in projects_to_process:
                    txn_tasks.append(self._call_force_complete_consensus(project, epochId))

            results = await asyncio.gather(*txn_tasks, return_exceptions=True)

            if self._finalized_epochs[epochId]:
                del self._finalized_epochs[epochId]
            if self._projects_submitted_for_epoch[epochId]:
                del self._projects_submitted_for_epoch[epochId]

            for result in results:
                if isinstance(result, Exception):
                    self._logger.error(
                        'Error while force completing consensus: {}', result,
                    )

    async def run(self):

        await self.setup()

        while True:
            try:
                current_block = await self.rpc_helper.get_current_block(redis_conn=self._writer_redis_pool)
                self._logger.info('Current block: {}', current_block)

            except Exception as e:
                self._logger.opt(exception=True).error(
                    (
                        'Unable to fetch current block, ERROR: {}, '
                        'sleeping for {} seconds.'
                    ),
                    e,
                    settings.anchor_chain.polling_interval,
                )

                await asyncio.sleep(settings.anchor_chain.polling_interval)
                continue

            # Only use redis is state is not locally present
            if not self._last_processed_block:
                last_processed_block_data = await self._reader_redis_pool.get(
                    event_detector_last_processed_block,
                )

                if last_processed_block_data:
                    self._last_processed_block = json.loads(
                        last_processed_block_data,
                    )
                else:
                    self._last_processed_block = current_block - 1

            if current_block - self._last_processed_block >= settings.anchor_chain.max_block_buffer:
                self._logger.warning(
                    'Last processed block is too far behind current block, '
                    'processing current block',
                )
                self._last_processed_block = current_block - settings.anchor_chain.max_block_buffer

            if self._last_processed_block == current_block:
                self._logger.info(
                    'No new blocks detected, sleeping for {} seconds...',
                    settings.anchor_chain.polling_interval,
                )
                await asyncio.sleep(settings.anchor_chain.polling_interval)
                continue
            # Get events from current block to last_processed_block
            try:
                await self.get_events(self._last_processed_block + 1, current_block)
            except Exception as e:
                self._logger.opt(exception=True).error(
                    (
                        'Unable to fetch events from block {} to block {}, '
                        'ERROR: {}, sleeping for {} seconds.'
                    ),
                    self._last_processed_block + 1,
                    current_block,
                    e,
                    settings.anchor_chain.polling_interval,
                )
                await asyncio.sleep(settings.anchor_chain.polling_interval)
                continue

            self._last_processed_block = current_block

            await self._writer_redis_pool.set(event_detector_last_processed_block, json.dumps(current_block))
            self._logger.info(
                'DONE: Processed blocks till, saving in redis: {}',
                current_block,
            )
            self._logger.info(
                'Sleeping for {} seconds...',
                settings.anchor_chain.polling_interval,
            )
            await asyncio.sleep(settings.anchor_chain.polling_interval)


def main():
    """Spin up the ticker process in event loop"""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(
        resource.RLIMIT_NOFILE,
        (settings.rlimit.file_descriptors, hard),
    )

    loop = uvloop.new_event_loop()
    asyncio.set_event_loop(loop)
    force_consensus_process = ForceConsensus()
    loop.run_until_complete(force_consensus_process.run())


if __name__ == '__main__':
    main()