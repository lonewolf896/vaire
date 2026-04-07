# Phase 4, Step 2: Remove Conflicting Compression (M18)

## The problem

Two independent compression systems exist:

### System 1: `MemoryCompressor` (compression.py)
- Runs during full consolidation cycle (idle)
- Three-level compression: full → gist → tag
- Archives original content before compressing
- Manages embedding tiering (float32 → int8 → FTS-only)
- Sets `compression_level` field (0, 1, 2)
- Respects `content_fidelity`, `is_protected`, `store_type`

### System 2: `SleepComputeEngine.compress_old_memories()` (sleep_compute.py)
- Runs during sleep cycle (6h+ gap)
- Simple heuristic: keep entity-bearing sentences
- Does NOT archive original content
- Does NOT set `compression_level` (sets `compressed=1` flag only)
- Does NOT respect `content_fidelity` or `store_type`
- Re-embeds with new content

### How they conflict

1. Sleep compression runs first (sleep cycle), sets `compressed=1`
2. `MemoryCompressor.compression_cycle()` runs next (full consolidation)
3. `MemoryCompressor` checks `compression_level` (still 0) and compresses again
4. But the content was already shortened by sleep compression
5. The gist extraction produces garbage from already-compressed content
6. No archive exists of the original (sleep compression didn't create one)

## Fix: Remove `compress_old_memories()` entirely

`MemoryCompressor` is the correct, full-featured system. It:
- Archives before compressing (decompression is possible)
- Tracks compression level properly
- Manages embedding tiering
- Respects `content_fidelity`, `is_protected`, `store_type` (including `reference`)

The sleep compression was an early implementation that predates `MemoryCompressor`.
It should be removed, and `MemoryCompressor` should be the sole compression path.

---

## Implementation

### 2a. Remove `compress_old_memories()` from sleep_compute.py

```
IN sleep_compute.py:

DELETE the entire method compress_old_memories() (lines ~437-484)

Also delete the module-level regex constants it uses (if not used elsewhere):
    _SENTENCE_RE
    _ENTITY_PATTERN_RE
```

Check if `_SENTENCE_RE` and `_ENTITY_PATTERN_RE` are used elsewhere in the file:

```
grep '_SENTENCE_RE\|_ENTITY_PATTERN_RE' vaire/sleep_compute.py
# If only used in compress_old_memories(), safe to delete
# If used in other methods, keep them
```

### 2b. Remove `compress_old_memories()` call from `run_sleep_cycle()`

```
IN sleep_compute.py, run_sleep_cycle():

BEFORE (line 505-506):
    logger.info("Sleep cycle phase 5: compression")
    stats["compressed"] = self.compress_old_memories()

AFTER:
    # Phase 5 removed: compression is handled by MemoryCompressor
    # during full consolidation cycles, which archives originals
    # and manages compression levels properly.
```

### 2c. Update stats in `run_sleep_cycle()`

```
BEFORE:
    def run_sleep_cycle(self) -> dict:
        stats: dict = {}
        
        logger.info("Sleep cycle phase 1: dream replay")
        stats["dream_replay"] = self.dream_replay()
        
        logger.info("Sleep cycle phase 2: community detection")
        stats["communities"] = self.detect_communities()
        
        logger.info("Sleep cycle phase 3: cluster summarization")
        self.generate_cluster_summaries()
        stats["cluster_summaries_generated"] = True
        
        logger.info("Sleep cycle phase 4: re-embedding")
        stats["reembedded"] = self.reembed_stale()
        
        logger.info("Sleep cycle phase 5: compression")       # REMOVE
        stats["compressed"] = self.compress_old_memories()     # REMOVE
        
        logger.info("Sleep cycle phase 6: auto-narrate")
        stats["narrative"] = self._narrative.auto_narrate()
        
        logger.info("Sleep cycle complete: %s", stats)
        return stats

AFTER:
    def run_sleep_cycle(self) -> dict:
        stats: dict = {}
        
        logger.info("Sleep cycle phase 1: dream replay")
        stats["dream_replay"] = self.dream_replay()
        
        logger.info("Sleep cycle phase 2: community detection")
        stats["communities"] = self.detect_communities()
        
        logger.info("Sleep cycle phase 3: cluster summarization")
        self.generate_cluster_summaries()
        stats["cluster_summaries_generated"] = True
        
        logger.info("Sleep cycle phase 4: re-embedding")
        stats["reembedded"] = self.reembed_stale()
        
        logger.info("Sleep cycle phase 5: auto-narrate")
        stats["narrative"] = self._narrative.auto_narrate()
        
        logger.info("Sleep cycle complete: %s", stats)
        return stats
```

### 2d. Check for external callers of `compress_old_memories`

```
grep -rn 'compress_old_memories' vaire/ --include='*.py' | grep -v __pycache__
# Expected: only sleep_compute.py itself (the method def + the call in run_sleep_cycle)
# If anything else calls it, those callers need updating too.
```
