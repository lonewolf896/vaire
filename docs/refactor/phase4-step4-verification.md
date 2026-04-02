# Phase 4, Step 4: Verification

## 4a. Double decay fix (M14)

### Unit test pseudocode

```
def test_decay_not_applied_twice_in_same_iteration():
    storage = StorageEngine(tmp_path / "test.db")
    settings = Settings(DB_PATH=..., DAEMON_CHECK_INTERVAL=5)
    engine = AstrocyteEngine(storage, mock_embeddings, settings)
    
    # Insert a memory with known heat
    mid = storage.insert_memory({
        "content": "test memory", "directory_context": "/test", "heat": 1.0
    })
    
    # Apply decay once
    stats1 = {"memories_updated": 0, "memories_archived": 0}
    engine._apply_decay(stats1)
    heat_after_first = storage.get_memory(mid)["heat"]
    
    # Apply decay again immediately (same iteration)
    stats2 = {"memories_updated": 0, "memories_archived": 0}
    engine._apply_decay(stats2)
    heat_after_second = storage.get_memory(mid)["heat"]
    
    # Second decay should be skipped — heat unchanged
    assert heat_after_second == heat_after_first
    assert stats2["memories_updated"] == 0  # nothing changed


def test_decay_applies_after_interval():
    storage = StorageEngine(tmp_path / "test.db")
    settings = Settings(DB_PATH=..., DAEMON_CHECK_INTERVAL=5)
    engine = AstrocyteEngine(storage, mock_embeddings, settings)
    
    mid = storage.insert_memory({
        "content": "test memory", "directory_context": "/test", "heat": 1.0
    })
    
    # Apply decay
    stats1 = {"memories_updated": 0, "memories_archived": 0}
    engine._apply_decay(stats1)
    heat_after_first = storage.get_memory(mid)["heat"]
    
    # Simulate time passing (> DAEMON_CHECK_INTERVAL)
    engine._last_decay_time -= timedelta(seconds=10)
    
    # Apply decay again — should run this time
    stats2 = {"memories_updated": 0, "memories_archived": 0}
    engine._apply_decay(stats2)
    heat_after_second = storage.get_memory(mid)["heat"]
    
    # Heat should have decayed further
    # (only if enough time passed for the memory itself to decay,
    #  which depends on last_accessed. The test may need to set
    #  last_accessed to something old enough.)


def test_force_consolidate_after_light_cycle():
    """force_consolidate right after a light cycle should not double-decay."""
    storage = StorageEngine(tmp_path / "test.db")
    settings = Settings(DB_PATH=...)
    engine = AstrocyteEngine(storage, mock_embeddings, settings)
    
    mid = storage.insert_memory({
        "content": "test memory", "directory_context": "/test", "heat": 1.0,
        "last_accessed": _hours_ago(48),  # old enough to actually decay
    })
    
    # Run light cycle (includes decay)
    engine._light_cycle()
    heat_after_light = storage.get_memory(mid)["heat"]
    
    # Immediately run force_consolidate (also includes decay)
    engine.force_consolidate()
    heat_after_force = storage.get_memory(mid)["heat"]
    
    # Heat should NOT have decayed twice
    assert heat_after_force == heat_after_light
```

---

## 4b. Compression removal (M18)

### Verification

```
# Verify compress_old_memories is fully removed
grep -rn 'compress_old_memories' vaire/ --include='*.py' | grep -v __pycache__ | grep -v test
# Expected: ZERO results (or only in docs/comments)

# Verify sleep cycle no longer calls compression
grep -n 'compress' vaire/sleep_compute.py | grep -v __pycache__
# Expected: no calls to compress_old_memories, only MemoryCompressor references if any

# Verify MemoryCompressor still runs during full consolidation
grep -n 'compression_cycle\|compress' vaire/consolidation.py | grep -v __pycache__
# Expected: _compressor.compression_cycle() still present in _consolidation_cycle()
```

### Behavioral check

```
# After the change, compression only happens via MemoryCompressor:
# 1. Full consolidation cycle (idle) → _compressor.compression_cycle()
# 2. Never during sleep cycle
# 3. MemoryCompressor always archives before compressing
# 4. MemoryCompressor respects content_fidelity, is_protected, store_type

# Verify with a long-running test:
# 1. Insert a memory with long content
# 2. Set its created_at to 31 days ago (past tag threshold)
# 3. Run force_consolidate() — should compress via MemoryCompressor
# 4. Verify archive exists: storage.get_archives_for_memory(mid) is not empty
# 5. Verify compression_level > 0
```

---

## 4c. Cluster cleanup (M19)

### Unit test pseudocode

```
def test_old_community_clusters_cleaned_up():
    storage = StorageEngine(tmp_path / "test.db")
    sleep_engine = SleepComputeEngine(storage, ...)
    
    # Create some entities and relationships so community detection produces results
    e1 = storage.insert_entity({"name": "entity_a", "type": "concept"})
    e2 = storage.insert_entity({"name": "entity_b", "type": "concept"})
    storage.insert_relationship({
        "source_entity_id": e1, "target_entity_id": e2,
        "relationship_type": "co_occurrence", "weight": 5.0,
    })
    
    # Insert memories mentioning these entities
    m1 = storage.insert_memory({
        "content": "content about entity_a and entity_b",
        "directory_context": "/test",
    })
    
    # Run community detection — creates cluster(s)
    results1 = sleep_engine.detect_communities()
    cluster_count_1 = len(results1)
    
    # Verify clusters exist
    clusters = storage.get_clusters_by_level(1)
    assert len([c for c in clusters if c["name"].startswith("community_")]) > 0
    
    # Run community detection AGAIN
    results2 = sleep_engine.detect_communities()
    
    # Old clusters should be cleaned up — total community cluster count
    # should be the same as one run, not doubled
    clusters = storage.get_clusters_by_level(1)
    community_clusters = [c for c in clusters if c["name"].startswith("community_")]
    assert len(community_clusters) == len(results2)  # Only current cycle's clusters


def test_cleanup_does_not_remove_non_community_clusters():
    storage = StorageEngine(tmp_path / "test.db")
    sleep_engine = SleepComputeEngine(storage, ...)
    
    # Create a non-community cluster (e.g., fractal tree cluster at level 1)
    custom_id = storage.insert_cluster({
        "name": "custom_analysis_cluster",
        "level": 1,
        "summary": "manually created",
    })
    
    # Run cleanup
    removed = sleep_engine._cleanup_community_clusters()
    
    # Custom cluster should still exist
    cluster = storage.get_cluster(custom_id)
    assert cluster is not None
    assert cluster["name"] == "custom_analysis_cluster"


def test_cleanup_unsets_memory_cluster_id():
    storage = StorageEngine(tmp_path / "test.db")
    sleep_engine = SleepComputeEngine(storage, ...)
    
    # Create a community cluster and assign a memory to it
    cid = storage.insert_cluster({
        "name": "community_0", "level": 1, "summary": "test",
    })
    mid = storage.insert_memory({
        "content": "test", "directory_context": "/test",
    })
    storage.execute_write(
        "UPDATE memories SET cluster_id = ? WHERE id = ?", (cid, mid)
    )
    
    # Verify assignment
    mem = storage.get_memory(mid)
    assert mem["cluster_id"] == cid
    
    # Run cleanup
    sleep_engine._cleanup_community_clusters()
    
    # Memory's cluster_id should be NULL
    mem = storage.get_memory(mid)
    assert mem["cluster_id"] is None
```

---

## 4d. Existing tests

```
# Run consolidation tests
.venv/bin/python -m pytest vaire/tests/test_consolidation.py -x -q

# Run sleep compute tests
.venv/bin/python -m pytest vaire/tests/test_sleep_compute.py -x -q

# Run compression tests (ensure MemoryCompressor still works)
.venv/bin/python -m pytest vaire/tests/test_compression.py -x -q

# Full suite
.venv/bin/python -m pytest vaire/tests/ -x -q \
    --ignore=vaire/tests/test_stress.py \
    --ignore=vaire/tests/test_live_system.py
```

---

## 4e. Issues resolved

| Issue | Status |
|---|---|
| M14 (double heat decay) | RESOLVED — timestamp-gated, at most once per check interval |
| M18 (conflicting compression) | RESOLVED — sleep compression removed, MemoryCompressor is sole owner |
| M19 (unstable cluster IDs) | RESOLVED — old community clusters cleaned up before new cycle |
