# Phase 4: Consolidation Phase Gating

## Context

Three issues in the consolidation daemon's lifecycle management:

### Problem M14: Double heat decay

In `_daemon_loop()`, a light cycle runs `_apply_decay()` every 60s. When the system
goes idle, `_consolidation_cycle()` also runs `_apply_decay()`. If both fire in the
same loop iteration (light cycle interval elapsed AND idle threshold crossed), decay
is applied twice, causing memories to decay faster than intended.

### Problem M18: Conflicting compression systems

`SleepComputeEngine.compress_old_memories()` (run during sleep cycle) compresses
memories using a simple sentence-extraction heuristic, sets `compressed=1`, but does
NOT archive the original. Meanwhile, `MemoryCompressor.compression_cycle()` (run
during full consolidation) uses a proper pipeline with archival, compression levels,
and embedding tiering. The two systems can fight — sleep compression bypasses the
archival safety net.

### Problem M19: Unstable cluster IDs

`detect_communities()` in sleep_compute.py always creates NEW cluster records and
reassigns `cluster_id` on memories. Old clusters are never cleaned up. Cluster IDs
are not stable across sleep cycles, so any references to clusters from prior cycles
become stale.

## Files Modified

| File | Change |
|---|---|
| vaire/consolidation.py | Add decay gating to prevent double application |
| vaire/sleep_compute.py | Remove `compress_old_memories()`, add cluster cleanup |

## Dependencies

None — independent of Phases 1-3.
