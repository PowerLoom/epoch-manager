from functools import wraps

def chunks(start_idx, stop_idx, n):
    run_idx = 0
    for i in range(start_idx, stop_idx + 1, n):
        # Create an index range for l of n items:
        begin_idx = i  # if run_idx == 0 else i+1
        if begin_idx == stop_idx + 1:
            return
        end_idx = i + n - 1 if i + n - 1 <= stop_idx else stop_idx
        run_idx += 1
        yield begin_idx, end_idx, run_idx


def semaphore_then_aiorwlock_aqcuire_release(fn):
    """
    A decorator that wraps a function and handles cleanup of any child processes
    spawned by the function in case of an exception.

    Args:
        fn (function): The function to be wrapped.

    Returns:
        function: The wrapped function.
    """
    @wraps(fn)
    async def wrapper(self, *args, **kwargs):
        await self._semaphore.acquire()
        try:
            await self._rwlock.writer_lock.acquire()
        except Exception as e:
            self._logger.error(f'Error acquiring rwlock: {e}. Releasing semaphore and exiting...')
            self._semaphore.release()
            raise e
        try:
            tx_hash = await fn(self, *args, **kwargs)
            # release semaphore
            try:
                self._semaphore.release()
            except Exception as e:
                self._logger.error(f'Error releasing semaphore: {e}. But moving on regardless...')
            # release rwlock
            try:
                self._rwlock.writer_lock.release()
            except Exception as e:
                self._logger.error(f'Error releasing rwlock: {e}. But moving on regardless...')
        except Exception as e:
            # this is ultimately reraised by tenacity once the retries are exhausted
            # nothing to do here
            raise e

        else:
            return tx_hash

    return wrapper