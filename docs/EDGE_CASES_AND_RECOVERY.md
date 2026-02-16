# Edge Cases and Recovery Guarantees

## Complete Edge Case Catalog

### EC1: forceSkipEpoch Timeout (Transaction Included)
**Scenario**: 
- Gap >= 10, force_skip enabled
- Submit forceSkipEpoch(24426899)
- Receipt wait times out (10s)
- Transaction actually succeeds on-chain

**Previous Behavior**:
- Receipt timeout → receipt = None
- Code treats receipt=None as success
- Updates begin_block_epoch = 24426899 + 1 = 24426900
- Next iteration: contract at 24426899, begin_block_epoch at 24426900
- Sync check: 24426900 > 24426899 → doesn't sync (WRONG)
- Tries releaseEpoch(24426899) → fails status=0 (epoch exists)
- Failure handler syncs → recovers accidentally

**Fixed Behavior**:
- Receipt timeout → receipt = None
- Code detects receipt=None → verifies contract state
- Contract at 24426899 → contract_next_epoch = 24426900 (currentEpoch.end + 1)
- contract_next_epoch > release_epoch['begin'] → 24426900 > 24426899 → TRUE → transaction included!
- Updates begin_block_epoch = 24426900
- Sends alert: EpochReleaseTimeout (transaction included)
- Continues sequentially from 24426900

**Recovery Guarantee**: ✅ Always recovers correctly

---

### EC2: releaseEpoch Timeout (Transaction Included)
**Scenario**:
- Sequential release of epoch N
- Submit releaseEpoch(N)
- Receipt wait times out
- Transaction actually succeeds on-chain

**Previous Behavior**:
- Receipt timeout → receipt = None
- Code treats receipt=None as success
- Updates begin_block_epoch = N + 1
- Next iteration: contract at N, begin_block_epoch at N+1
- Sync check: N+1 > N → doesn't sync (WRONG)
- Tries releaseEpoch(N) → fails status=0 (epoch exists)
- Failure handler syncs → recovers accidentally

**Fixed Behavior**:
- Receipt timeout → receipt = None
- Code detects receipt=None → verifies contract state
- Contract at N → contract_next_epoch = N + 1 (currentEpoch.end + 1)
- contract_next_epoch > release_epoch['begin'] → N + 1 > N → TRUE → transaction included!
- Updates begin_block_epoch = N + 1
- Sends alert: EpochReleaseTimeout (transaction included)
- Continues sequentially

**Recovery Guarantee**: ✅ Always recovers correctly

---

### EC3: releaseEpoch Timeout (Transaction NOT Included)
**Scenario**:
- Sequential release of epoch N
- Submit releaseEpoch(N)
- Receipt wait times out
- Transaction NOT included (RPC issue, network problem)

**Previous Behavior**:
- Receipt timeout → receipt = None
- Code treats receipt=None as success
- Updates begin_block_epoch = N + 1
- Next iteration: contract still at N-1, begin_block_epoch at N+1
- Tries releaseEpoch(N+1) → fails (epoch N not released)
- Creates gap

**Fixed Behavior**:
- Receipt timeout → receipt = None
- Code detects receipt=None → verifies contract state
- Contract at N-1 → contract_next_epoch = N (currentEpoch.end + 1)
- contract_next_epoch <= release_epoch['begin'] → N <= N → TRUE → transaction NOT included
- Keeps begin_block_epoch = N
- Refreshes nonce
- Sends alert: EpochReleaseTimeout (transaction NOT included, will retry)
- Retries in next iteration

**Recovery Guarantee**: ✅ Always retries correctly

---

### EC4: Nonce Too Low Error
**Scenario**:
- Submit transaction with nonce N
- Receipt timeout
- Transaction included with nonce N
- Next transaction uses nonce N → "nonce too low"

**Behavior**:
- Exception caught → detect "nonce too low"
- Refresh nonce from chain
- Sync begin_block_epoch to contract_next_epoch
- Break to recalculate

**Recovery Guarantee**: ✅ Always recovers correctly

---

### EC5: forceSkipEpoch Permission Denied
**Scenario**:
- Gap >= 10, force_skip enabled
- Submit forceSkipEpoch
- Transaction fails status=0 (permission denied)

**Behavior**:
- Receipt status=0 detected
- Fallback to releaseEpoch(contract_next_epoch)
- Continue sequentially

**Recovery Guarantee**: ✅ Always falls back correctly

---

### EC6: Multiple Epoch Managers (Race Condition)
**Scenario**:
- Two epoch managers running simultaneously
- Both try to release epoch N
- One succeeds, other gets E22

**Behavior**:
- Receipt status=0 detected
- Sync begin_block_epoch to contract_next_epoch
- Continue with next epoch

**Recovery Guarantee**: ✅ Always syncs correctly

---

### EC7: Contract Sync After Gap Detection
**Scenario**:
- Gap >= 10, force_skip disabled
- Sync begin_block_epoch to contract_next_epoch
- Contract at N, begin_block_epoch now N
- Process epochs sequentially from N

**Behavior**:
- Sync happens once before processing loop
- Continues sequentially from contract state
- No duplicate releases

**Recovery Guarantee**: ✅ Always syncs correctly

---

### EC8: Gap Detection After forceSkipEpoch Success
**Scenario**:
- forceSkipEpoch succeeds, jumps to head
- Next poll: gap still >= 10 (chain advanced)
- Should continue sequentially, not forceSkipEpoch again

**Behavior**:
- After forceSkipEpoch success, begin_block_epoch updated
- Next iteration: gap recalculated
- If gap still >= 10 and force_skip enabled, may try again
- But contract state prevents duplicates

**Recovery Guarantee**: ✅ Contract state prevents duplicates

---

## Recovery Path Matrix

| Error Type | Detection | Action | Alert Sent | Recovery |
|------------|-----------|--------|------------|----------|
| Receipt timeout, tx included | Contract verification | Update begin_block_epoch | ✅ EpochReleaseTimeout | ✅ Immediate |
| Receipt timeout, tx not included | Contract verification | Keep begin_block_epoch, retry | ✅ EpochReleaseTimeout | ✅ Next iteration |
| Receipt status=0, releaseEpoch | Receipt check | Sync to contract | ✅ EpochReleaseTxnFailed | ✅ Immediate |
| Receipt status=0, forceSkipEpoch | Receipt check | Fallback to releaseEpoch | ✅ EpochReleaseTxnFailed | ✅ Immediate |
| Nonce too low | Exception check | Refresh nonce, sync | ❌ None | ✅ Immediate |
| Other exception | Exception handler | Sync to contract, wait 30s | ✅ EpochReleaseError | ✅ Next iteration |

## Invariants Maintained

1. **begin_block_epoch >= contract_next_epoch** (never release existing epochs)
2. **After success: begin_block_epoch = release_epoch['end'] + 1** (sequential continuation)
3. **After error: begin_block_epoch = contract_next_epoch** (sync to contract)
4. **After timeout: verify contract state** (don't assume transaction failed)
5. **Nonce always fetched from chain** (never increment locally)

## Formal Verification Checklist

- [x] All timeout paths verify contract state
- [x] All error paths sync to contract
- [x] All success paths update begin_block_epoch sequentially
- [x] No duplicate epoch releases possible
- [x] No infinite loops possible
- [x] All edge cases have recovery paths
- [x] State invariants maintained in all paths
