# Phase 4, Step 3: Stable Cluster IDs (M19)

## The problem

`detect_communities()` in sleep_compute.py:
1. Runs Louvain community detection on the entity graph
2. Always calls `insert_cluster()` — creates NEW cluster records
3. Reassigns `cluster_id` on memories to the NEW cluster IDs
4. Never cleans up old cluster records

After 10 sleep cycles, there are 10 generations of cluster records in
`memory_clusters`, all but the latest orphaned. Cluster IDs are not stable
across cycles — any code that cached a cluster_id (fractal tree navigation,
drill_down tool) gets stale references.

## Fix: Clean up old clusters before creating new ones

Before creating new clusters, delete the ones from the previous cycle.
This keeps the `memory_clusters` table lean and ensures only current
clusters exist.

### Design choice: delete-then-recreate vs update-in-place

**Delete-then-recreate** (chosen):
- Simple: wipe old community clusters, create new ones
- Works even if community structure changes drastically
- Downside: cluster IDs change every cycle (but they already did)

**Update-in-place** (rejected):
- Would need to match old clusters to new communities by membership overlap
- Complex, fragile, and the matching is ambiguous for split/merge communities
- Not worth the complexity for a system that runs every 6+ hours

### Which clusters to delete?

Only clusters created by `detect_communities()` — identified by name pattern
`community_*` and level=1. Do NOT delete fractal tree clusters (level 0, 2, 3)
or user-created clusters.

---

## Implementation

```
IN sleep_compute.py, detect_communities():

BEFORE (line ~246, after community detection, before creating clusters):
    results = []
    for comm_idx, community in enumerate(communities):
        ...
        cluster_id = self._storage.insert_cluster(...)
        ...

AFTER:
    # Clean up previous community clusters before creating new ones
    self._cleanup_community_clusters()
    
    results = []
    for comm_idx, community in enumerate(communities):
        ...
        cluster_id = self._storage.insert_cluster(...)
        ...
```

```
IN sleep_compute.py, add method:

METHOD _cleanup_community_clusters(self) -> int:
    """Remove community clusters from previous sleep cycles.
    
    Only removes clusters with name matching 'community_*' at level 1.
    Unsets cluster_id on memories that referenced them.
    Returns count of clusters removed.
    """
    # Find old community clusters
    old_clusters = self._storage.get_clusters_by_level(1)
    removed = 0
    
    for cluster in old_clusters:
        IF not cluster.get("name", "").startswith("community_"):
            continue
        
        cluster_id = cluster["id"]
        
        # Unset cluster_id on memories that referenced this cluster
        self._storage.execute_write(
            "UPDATE memories SET cluster_id = NULL WHERE cluster_id = ?",
            (cluster_id,),
        )
        
        # Delete the cluster record
        self._storage.execute_write(
            "DELETE FROM memory_clusters WHERE id = ?",
            (cluster_id,),
        )
        
        removed += 1
    
    IF removed > 0:
        logger.info("Cleaned up %d old community clusters", removed)
    
    RETURN removed
```

### Note on `get_clusters_by_level`

This method already exists in StorageEngine:
```
def get_clusters_by_level(self, level: int) -> list[dict]:
```
So no new StorageEngine method is needed.

### Note on `execute_write` usage

The cleanup uses `execute_write` (which holds the write lock) for the
UPDATE and DELETE operations. This is correct and thread-safe.

After Phase 1, if `_conn` is renamed to `__conn`, `execute_write` still
works the same way since it's a public StorageEngine method.

---

## Alternative considered: also stabilize cluster IDs across runs

We could hash the community members to create deterministic cluster IDs,
so the same community always gets the same ID. This would make `drill_down`
references stable across sleep cycles.

Rejected for now because:
1. Louvain community detection is non-deterministic (depends on node ordering)
   even with `seed=42`, because the entity graph changes between cycles
2. Communities split and merge — the "same" community doesn't exist
3. `drill_down` is a real-time exploration tool, not a bookmark system
4. Keeping it simple (clean slate each cycle) is more predictable

If cluster stability becomes important later, a better approach would be
to assign semantic names (e.g., "auth-subsystem", "deployment-pipeline")
based on entity content, rather than trying to stabilize arbitrary IDs.
