# Epoch Release Logic Specification

## State Variables

- `begin_block_epoch`: Next epoch to release (local state)
- `contract_epoch_end`: Last epoch released on contract (from contract)
- `contract_next_epoch`: contract_epoch_end + 1 (next sequential epoch)
- `cur_block`: Current chain head block
- `end_block_epoch`: cur_block - head_offset (target end block)
- `gap_from_head`: cur_block - begin_block_epoch (how far behind we are)
- `use_force_skip`: Boolean flag for intentional jump ahead

## Core Invariants

1. **Sequential Release Invariant**: After releasing epoch N, next epoch MUST be N+1 (unless forceSkipEpoch)
2. **Contract Sync Invariant**: begin_block_epoch MUST be >= contract_next_epoch (never release epochs that already exist)
3. **Nonce Invariant**: Nonce MUST be fetched from chain before each transaction (never increment locally)
4. **State Recovery Invariant**: After ANY error/timeout, verify contract state before proceeding

## State Machine

```
INIT → [Fetch contract epoch] → SYNC_STATE
SYNC_STATE → [Calculate gap] → GAP_CHECK
GAP_CHECK → [gap < threshold] → SEQUENTIAL_RELEASE
GAP_CHECK → [gap >= threshold, force_skip_enabled] → FORCE_SKIP
GAP_CHECK → [gap >= threshold, force_skip_disabled] → SYNC_TO_CONTRACT
SEQUENTIAL_RELEASE → [Release epoch N] → [Success] → UPDATE_STATE(N+1) → GAP_CHECK
SEQUENTIAL_RELEASE → [Release epoch N] → [Failure] → ERROR_HANDLER → SYNC_STATE
FORCE_SKIP → [forceSkipEpoch to head] → [Success] → UPDATE_STATE(head) → GAP_CHECK
FORCE_SKIP → [forceSkipEpoch to head] → [Timeout] → VERIFY_CONTRACT → SYNC_STATE
FORCE_SKIP → [forceSkipEpoch to head] → [Failure] → FALLBACK_RELEASE → SEQUENTIAL_RELEASE
```

## Decision Tree

### 1. Gap Detection (Every Poll)

```
IF gap_from_head >= GAP_THRESHOLD (10 blocks):
    IF force_skip_enabled:
        use_force_skip = True
        begin_block_epoch = cur_block - head_offset - GAP_OFFSET
    ELSE:
        use_force_skip = False
        begin_block_epoch = contract_next_epoch  // Sync to contract
ELSE:
    use_force_skip = False
    // Continue with current begin_block_epoch
```

### 2. Pre-Processing Sync (Before For Loop)

```
IF NOT use_force_skip:
    contract_next_epoch = fetch_contract_epoch()
    IF begin_block_epoch < contract_next_epoch:
        begin_block_epoch = contract_next_epoch  // Sync once
```

### 3. Epoch Release Decision (Inside For Loop)

```
IF use_force_skip:
    function_name = 'forceSkipEpoch'
    release_epoch = epoch_block  // Use calculated epoch
ELSE:
    function_name = 'releaseEpoch'
    release_epoch = epoch_block  // Sequential release
```

### 4. Transaction Submission

```
nonce = get_transaction_count(address)  // Always fetch from chain
tx_hash = submit_transaction(release_epoch)
receipt = wait_for_receipt(tx_hash, timeout=10s)  // Non-blocking
```

### 5. Result Handling

```
IF receipt exists AND receipt.status == 1:
    // CONFIRMED SUCCESS
    begin_block_epoch = release_epoch['end'] + 1
    refresh_nonce()
    break  // Recalculate end_block_epoch from chain head
    
ELIF receipt exists AND receipt.status == 0:
    // CONFIRMED FAILURE
    IF function_name == 'releaseEpoch':
        // E22 or other error - sync to contract
        begin_block_epoch = contract_next_epoch
        refresh_nonce()
        break
    ELSE:
        // forceSkipEpoch failed - try fallback
        retry with releaseEpoch(contract_next_epoch)
        
ELSE (receipt is None - timeout during receipt wait):
    // TIMEOUT - MUST verify if transaction was included
    // fetch_contract_epoch() returns currentEpoch.end + 1 (next epoch to release)
    contract_next_epoch = fetch_contract_epoch()
    IF contract_next_epoch > release_epoch['begin']:
        // Transaction was included! Contract advanced past what we tried to release
        begin_block_epoch = contract_next_epoch
        refresh_nonce()
        break
    ELSE:
        // Transaction not included - contract_next_epoch <= release_epoch['begin']
        // Keep begin_block_epoch unchanged, will retry
        refresh_nonce()
        break  // Will retry in next iteration
```

### 6. Exception Handling

```
IF nonce_error:
    refresh_nonce()
    contract_next_epoch = fetch_contract_epoch()
    begin_block_epoch = contract_next_epoch
    break
    
IF timeout_error:
    refresh_nonce()
    transaction_included = False
    contract_next_epoch = fetch_contract_epoch()
    IF contract_next_epoch > release_epoch['begin']:
        transaction_included = True
        begin_block_epoch = contract_next_epoch
    ELSE:
        transaction_included = False
        // Keep begin_block_epoch unchanged
    send_alert('EpochReleaseTimeout', transaction_included status)
    break
    
IF other_error:
    notify()
    wait(30s)
    refresh_nonce()
    contract_next_epoch = fetch_contract_epoch()
    begin_block_epoch = contract_next_epoch
    break
```

## Edge Cases

### Edge Case 1: forceSkipEpoch Timeout
**Scenario**: forceSkipEpoch times out, but transaction was included
**Previous Behavior**: Doesn't check contract, tries releaseEpoch, fails, then syncs
**Fixed Behavior**: After timeout, check contract. If included, update begin_block_epoch and continue
**Status**: ✅ FIXED - Contract verification added after receipt timeout

### Edge Case 2: Sequential Release Timeout
**Scenario**: releaseEpoch times out, transaction may or may not be included
**Previous Behavior**: Assumes not included, continues with same begin_block_epoch
**Fixed Behavior**: Check contract. If included, update begin_block_epoch. If not, retry.
**Status**: ✅ FIXED - Contract verification added after receipt timeout

### Edge Case 3: Contract Sync Race Condition
**Scenario**: Multiple epoch managers releasing simultaneously
**Current Behavior**: E22 error, syncs to contract
**Correct Behavior**: E22 is expected, sync and continue (already handled)

### Edge Case 4: Gap Detection After forceSkipEpoch
**Scenario**: forceSkipEpoch succeeds, but next poll still shows gap
**Current Behavior**: May try forceSkipEpoch again
**Correct Behavior**: After forceSkipEpoch success, should continue sequentially

### Edge Case 5: Nonce Drift
**Scenario**: Transaction included but receipt timeout
**Current Behavior**: Fetches nonce from chain (correct)
**Correct Behavior**: Already correct

## Recovery Guarantees

After ANY error/timeout:
1. Fetch contract state
2. Sync begin_block_epoch to contract_next_epoch if behind
3. Refresh nonce from chain
4. Break to recalculate end_block_epoch

This ensures we never get stuck in a loop and always recover to correct state.

## Analysis of Logged Incident

**What Happened:**
1. Contract at 24426722, chain head at 24426900 (177 block gap)
2. Gap >= 10, force_skip_enabled=True → use_force_skip=True
3. Set begin_block_epoch = 24426899 (jump to near head)
4. Submit forceSkipEpoch(24426899) → RPC timeout (tx submitted but receipt wait timed out)
5. Timeout handler broke without checking contract
6. Next iteration: begin_block_epoch still 24426899, contract still at 24426722
7. Sync check: 24426899 > 24426723 → doesn't sync (WRONG - should check contract after timeout)
8. Tries releaseEpoch(24426899) → fails status=0 (epoch already exists - forceSkipEpoch succeeded!)
9. Failure handler syncs to contract → finds 24426899
10. Continues sequentially from 24426900

**Why It Recovered:**
- Only recovered because releaseEpoch failed and triggered sync
- If releaseEpoch had succeeded (wrong epoch), would have created gap
- Recovery was accidental, not guaranteed

**Fix Applied:**
- After timeout, verify contract state before proceeding
- If transaction was included, update begin_block_epoch accordingly
- This ensures guaranteed recovery path
