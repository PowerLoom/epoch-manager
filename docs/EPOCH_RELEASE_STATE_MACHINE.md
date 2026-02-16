# Epoch Release State Machine Diagram

```mermaid
stateDiagram-v2
    [*] --> Init: Start
    Init --> FetchContract: Get contract state
    FetchContract --> CalculateGap: Calculate gap_from_head
    
    CalculateGap --> LargeGapCheck: gap >= 10?
    LargeGapCheck --> ForceSkipEnabled: force_skip_enabled?
    LargeGapCheck --> SyncToContract: gap >= 10, force_skip disabled
    LargeGapCheck --> SequentialRelease: gap < 10
    
    ForceSkipEnabled --> PreSyncCheck: use_force_skip = True
    SyncToContract --> PreSyncCheck: Sync begin_block_epoch
    SequentialRelease --> PreSyncCheck: Continue sequential
    
    PreSyncCheck --> SyncIfNeeded: NOT use_force_skip?
    SyncIfNeeded --> ProcessEpochs: Sync begin_block_epoch if needed
    PreSyncCheck --> ProcessEpochs: use_force_skip (skip sync)
    
    ProcessEpochs --> DetermineFunction: For each epoch in chunks
    DetermineFunction --> ForceSkip: use_force_skip?
    DetermineFunction --> ReleaseEpoch: Sequential release
    
    ForceSkip --> SubmitTx: Submit forceSkipEpoch
    ReleaseEpoch --> SubmitTx: Submit releaseEpoch
    
    SubmitTx --> WaitReceipt: Fire-and-forget
    WaitReceipt --> CheckReceipt: Try get receipt (10s timeout)
    
    CheckReceipt --> Success: receipt.status == 1
    CheckReceipt --> Failure: receipt.status == 0
    CheckReceipt --> ReceiptTimeout: receipt is None
    
    Success --> UpdateState: begin_block_epoch = release_epoch['end'] + 1
    UpdateState --> Recalculate: Break, recalculate end_block_epoch
    Recalculate --> CalculateGap: Next iteration
    
    Failure --> CheckFunction: Which function failed?
    CheckFunction --> SyncAndContinue: releaseEpoch failed (E22)
    CheckFunction --> FallbackRelease: forceSkipEpoch failed
    
    SyncAndContinue --> FetchContract: Sync to contract_next_epoch
    FallbackRelease --> RetryReleaseEpoch: Try releaseEpoch fallback
    RetryReleaseEpoch --> CheckReceipt: Wait for receipt
    
    ReceiptTimeout --> VerifyContract: CRITICAL: Check if tx was included
    VerifyContract --> ContractAhead: contract_next_epoch > release_epoch['begin']
    VerifyContract --> ContractEqual: contract_next_epoch == release_epoch['begin']
    VerifyContract --> ContractBehind: contract_next_epoch < release_epoch['begin']
    
    ContractAhead --> UpdateState: begin_block_epoch = contract_next_epoch
    ContractEqual --> UpdateState: begin_block_epoch = contract_next_epoch + 1
    ContractBehind --> Recalculate: Keep begin_block_epoch, retry
    
    note right of ReceiptTimeout
        CRITICAL FIX: receipt=None was
        previously treated as success.
        Now always verifies contract
        state before proceeding.
    end note
    
    note right of VerifyContract
        CRITICAL: Always verify contract
        state after timeout to avoid
        duplicate releases
    end note
    
    note right of UpdateState
        CRITICAL: Always update
        begin_block_epoch to continue
        sequentially. Never reset to
        contract unless error.
    end note
```

## State Transitions

### Normal Flow (Sequential Release)
```
CalculateGap → SequentialRelease → ProcessEpochs → ReleaseEpoch → 
SubmitTx → WaitReceipt → Success → UpdateState → Recalculate → CalculateGap
```

### Catch-Up Flow (Large Gap, Sequential)
```
CalculateGap → LargeGapCheck → SyncToContract → PreSyncCheck → 
SyncIfNeeded → ProcessEpochs → ReleaseEpoch → SubmitTx → Success → 
UpdateState → Recalculate → CalculateGap (repeat until caught up)
```

### Jump Flow (Large Gap, Force Skip)
```
CalculateGap → LargeGapCheck → ForceSkipEnabled → PreSyncCheck → 
ProcessEpochs → ForceSkip → SubmitTx → WaitReceipt → Success/Timeout → 
VerifyContract → UpdateState → Recalculate → CalculateGap
```

### Error Recovery Flow
```
AnyError → VerifyContract → UpdateState → Recalculate → CalculateGap
```

## Critical Paths

### Path 1: Successful Sequential Release
1. Calculate gap (gap < 10)
2. Sync to contract if needed (once)
3. Process epochs sequentially
4. Submit releaseEpoch(N)
5. Get receipt (status=1)
6. Update begin_block_epoch = N+1
7. Break, recalculate end_block_epoch
8. Repeat

### Path 2: Catch-Up After Gap
1. Calculate gap (gap >= 10)
2. Sync begin_block_epoch to contract_next_epoch
3. Process epochs sequentially from contract_next_epoch
4. Submit releaseEpoch(N), releaseEpoch(N+1), ...
5. After each success: begin_block_epoch = N+1, N+2, ...
6. Continue until gap < 10

### Path 3: Force Skip Recovery
1. Calculate gap (gap >= 10, force_skip enabled)
2. Set use_force_skip = True
3. Submit forceSkipEpoch to head
4. If timeout: Verify contract state
5. If included: Update begin_block_epoch
6. Continue sequentially

## Invariants to Maintain

1. **begin_block_epoch >= contract_next_epoch** (never release existing epochs)
2. **After success: begin_block_epoch = release_epoch['end'] + 1** (sequential continuation)
3. **After error: begin_block_epoch = contract_next_epoch** (sync to contract)
4. **Nonce always fetched from chain** (never increment locally)
5. **After timeout: verify contract state** (don't assume transaction failed)
