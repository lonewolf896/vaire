# Step 2: Migration of External _conn Call Sites

Each section shows BEFORE (current code) → AFTER (using new domain methods).
All changes are mechanical replacements — no logic changes.

---

## 2a. server.py (5 sites)

```
# Site 1: line ~1076 — sum reconsolidation count
BEFORE:
    reconsolidation_total = storage._conn.execute(
        "SELECT COALESCE(SUM(reconsolidation_count), 0) FROM memories"
    ).fetchone()[0]
AFTER:
    reconsolidation_total = storage.sum_reconsolidation_count()


# Site 2: line ~1100 — count episodic memories
BEFORE:
    ep_count = storage._conn.execute(
        "SELECT COUNT(*) FROM memories WHERE store_type = 'episodic' AND heat > 0"
    ).fetchone()[0]
AFTER:
    ep_count = storage.count_memories(store_type="episodic", min_heat=0.0)


# Site 3: line ~1103 — count semantic memories
BEFORE:
    sem_count = storage._conn.execute(
        "SELECT COUNT(*) FROM memories WHERE store_type = 'semantic' AND heat > 0"
    ).fetchone()[0]
AFTER:
    sem_count = storage.count_memories(store_type="semantic", min_heat=0.0)


# Site 4: line ~1111 — count by compression level (inside a loop)
BEFORE:
    comp_count = storage._conn.execute(
        "SELECT COUNT(*) FROM memories WHERE compression_level = ? AND heat > 0",
        (level,)
    ).fetchone()[0]
AFTER:
    comp_count = storage.count_memories(compression_level=level, min_heat=0.0)


# Site 5: line ~1124 — count causal relationships
BEFORE:
    causal_count = storage._conn.execute(
        "SELECT COUNT(*) FROM relationships WHERE is_causal = 1"
    ).fetchone()[0]
AFTER:
    causal_count = storage.count_causal_relationships()
```

---

## 2b. consolidation.py (2 sites)

```
# Site 1: line ~154 — load last sleep cycle from metadata
BEFORE:
    row = self._storage._conn.execute(
        "SELECT value FROM metadata WHERE key = 'last_sleep_cycle'"
    ).fetchone()
    if row:
        dt = datetime.fromisoformat(row[0])
        ...
AFTER:
    val = self._storage.get_metadata_value("last_sleep_cycle")
    if val:
        dt = datetime.fromisoformat(val)
        ...


# Site 2: line ~756 — get unprocessed action log entries
BEFORE:
    rows = self._storage._conn.execute(
        "SELECT id, tool_name, tool_input_summary, directory, timestamp "
        "FROM action_log WHERE processed = 0 "
        "ORDER BY timestamp ASC LIMIT 200"
    ).fetchall()
    for row in rows:
        entry_id = row[0]
        tool_name = row[1]
        summary = row[2]
        directory = row[3]
        timestamp = row[4]
AFTER:
    entries = self._storage.get_unprocessed_action_log(limit=200)
    for entry in entries:
        entry_id = entry["id"]
        tool_name = entry["tool_name"]
        summary = entry["tool_input_summary"]
        directory = entry["directory"]
        timestamp = entry["timestamp"]
```

---

## 2c. curation.py (9 sites)

```
# Site 1: line ~380 — get memories for pruning
BEFORE:
    rows = self._storage._conn.execute(
        "SELECT id FROM memories WHERE heat < 0.01 AND confidence < 0.3 "
        "AND access_count = 0 AND store_type != 'reference'"
    ).fetchall()
    for row in rows:
        self._storage.delete_memory(row[0])
AFTER:
    memories = self._storage.get_memories_for_pruning()
    for mem in memories:
        self._storage.delete_memory(mem["id"])


# Site 2: line ~391 — get memories for strengthening
BEFORE:
    rows = self._storage._conn.execute(
        "SELECT id, importance FROM memories "
        "WHERE access_count > 5 AND confidence > 0.8 AND importance < 1.0 "
        "AND store_type != 'reference'"
    ).fetchall()
    for row in rows:
        mem_id = row[0]
        current_importance = row[1] if row[1] is not None else 0.5
AFTER:
    memories = self._storage.get_memories_for_strengthening()
    for mem in memories:
        mem_id = mem["id"]
        current_importance = mem.get("importance") or 0.5


# Site 3: line ~417 — get relationships for reweighting
BEFORE:
    rows = self._storage._conn.execute(
        "SELECT r.id, r.weight, r.source_entity_id, r.target_entity_id "
        "FROM relationships r"
    ).fetchall()
    for row in rows:
        rel_id, weight, src_id, tgt_id = row[0], row[1], row[2], row[3]
AFTER:
    rels = self._storage.get_relationships_by_weight(min_weight=0.0)
    for rel in rels:
        rel_id = rel["id"]
        weight = rel.get("weight") or 1.0
        src_id = rel["source_entity_id"]
        tgt_id = rel["target_entity_id"]


# Site 4: line ~428 — get source entity heat
BEFORE:
    src = self._storage._conn.execute(
        "SELECT heat FROM entities WHERE id = ?", (src_id,)
    ).fetchone()
    if src is None: continue
    src_heat = src[0] if src[0] is not None else 0.0
AFTER:
    src_heat_val = self._storage.get_entity_heat(src_id)
    if src_heat_val is None: continue
    src_heat = src_heat_val


# Site 5: line ~431 — get target entity heat
BEFORE:
    tgt = self._storage._conn.execute(
        "SELECT heat FROM entities WHERE id = ?", (tgt_id,)
    ).fetchone()
    if tgt is None: continue
    tgt_heat = tgt[0] if tgt[0] is not None else 0.0
AFTER:
    tgt_heat_val = self._storage.get_entity_heat(tgt_id)
    if tgt_heat_val is None: continue
    tgt_heat = tgt_heat_val


# Site 6: line ~463 — get co-occurrence relationships for derivation
BEFORE:
    rows = self._storage._conn.execute(
        "SELECT r.source_entity_id, r.target_entity_id, r.weight "
        "FROM relationships r "
        "WHERE r.weight > 5.0 AND r.relationship_type = 'co_occurrence'"
    ).fetchall()
    for row in rows:
        src_id, tgt_id, weight = row[0], row[1], row[2]
AFTER:
    rels = self._storage.get_relationships_by_weight(
        min_weight=5.0, relationship_type="co_occurrence"
    )
    for rel in rels:
        src_id = rel["source_entity_id"]
        tgt_id = rel["target_entity_id"]
        weight = rel["weight"]

    # NOTE: Original uses "weight > 5.0" but get_relationships_by_weight uses
    # "weight >= ?". There is a secondary check at line ~472: "if weight < 10.0: continue"
    # so using >= 5.0 is safe (returns a superset, filtered by the existing check).


# Site 7: line ~476 — get source entity name
BEFORE:
    src_entity = self._storage._conn.execute(
        "SELECT name FROM entities WHERE id = ?", (src_id,)
    ).fetchone()
    if src_entity is None: continue
    src_name = src_entity[0]
AFTER:
    src_name = self._storage.get_entity_name(src_id)
    if src_name is None: continue


# Site 8: line ~479 — get target entity name
BEFORE:
    tgt_entity = self._storage._conn.execute(
        "SELECT name FROM entities WHERE id = ?", (tgt_id,)
    ).fetchone()
    if tgt_entity is None: continue
    tgt_name = tgt_entity[0]
AFTER:
    tgt_name = self._storage.get_entity_name(tgt_id)
    if tgt_name is None: continue


# Site 9: line ~497 — check if derived fact already exists
BEFORE:
    existing = self._storage._conn.execute(
        "SELECT id FROM memories WHERE content = ?", (derived_content,)
    ).fetchone()
    if existing: continue
AFTER:
    if self._storage.memory_exists_with_content(derived_content):
        continue
```

---

## 2d. retrieval.py (7 sites, 6 after FTS/LIKE merge)

```
# Site 1: line ~854 — get entity name for PPR ranking
BEFORE:
    row = self._storage._conn.execute(
        "SELECT name FROM entities WHERE id = ?", (eid,)
    ).fetchone()
    name = row[0] if row else None
AFTER:
    name = self._storage.get_entity_name(eid)


# Site 2: line ~949 — get entity name for spreading activation
BEFORE:
    row = self._storage._conn.execute(
        "SELECT name FROM entities WHERE id = ?", (neighbor_id,)
    ).fetchone()
    neighbor_name = row[0] if row else ""
AFTER:
    neighbor_name = self._storage.get_entity_name(neighbor_id) or ""


# Site 3: line ~1193 — get memory IDs for HDC candidates
BEFORE:
    rows = self._storage._conn.execute(
        "SELECT id FROM memories WHERE heat > ? LIMIT ?",
        (min_heat, limit)
    ).fetchall()
    candidate_ids = [r[0] for r in rows]
AFTER:
    candidate_ids = self._storage.get_memory_ids_by_heat(min_heat, limit)


# Site 4: line ~1201 — get HDC vector for a memory
BEFORE:
    row = self._storage._conn.execute(
        "SELECT hdc_vector FROM memories WHERE id = ?", (mid,)
    ).fetchone()
    hdc_vec = row[0] if row else None
AFTER:
    hdc_vec = self._storage.get_hdc_vector(mid)


# Sites 5-6: lines ~2378, ~2387 — find memories for entity (FTS + LIKE fallback)
BEFORE:
    # FTS attempt:
    try:
        rows = self._storage._conn.execute(
            "SELECT m.id FROM memories m "
            "JOIN memories_fts fts ON m.id = fts.rowid "
            "WHERE memories_fts MATCH ? AND m.heat > 0",
            (fts_query,)
        ).fetchall()
    except: pass
    # LIKE fallback:
    rows = self._storage._conn.execute(
        "SELECT id FROM memories WHERE content LIKE ? AND heat > 0",
        (f"%{name}%",)
    ).fetchall()
AFTER:
    mem_ids = self._storage.find_memories_mentioning(entity_name)
    # Both FTS and LIKE are handled inside the domain method


# Site 7: line ~2417 — get entity name for co-occurrence lookup
BEFORE:
    row = self._storage._conn.execute(
        "SELECT name FROM entities WHERE id = ?", (entity_id,)
    ).fetchone()
    name = row[0] if row else None
AFTER:
    name = self._storage.get_entity_name(entity_id)
```

---

## 2e. knowledge_graph.py (10 sites)

```
# Site 1: line ~158 — get relationships at time
BEFORE:
    rows = self._storage._conn.execute(
        "SELECT r.*, e1.name AS source_name, e2.name AS target_name "
        "FROM relationships r "
        "JOIN entities e1 ON e1.id = r.source_entity_id "
        "JOIN entities e2 ON e2.id = r.target_entity_id "
        "WHERE (r.source_entity_id = ? OR r.target_entity_id = ?) "
        "AND r.event_time <= ?",
        (entity_id, entity_id, time_str)
    ).fetchall()
AFTER:
    rows = self._storage.get_relationships_at_time(entity_id, time_str)


# Site 2: line ~179 — get relationship history
BEFORE:
    rows = self._storage._conn.execute(
        "SELECT r.*, e1.name ..., e2.name ... "
        "WHERE (src=? AND tgt=?) OR (src=? AND tgt=?)",
        (id_a, id_b, id_b, id_a)
    ).fetchall()
AFTER:
    rows = self._storage.get_relationship_history(id_a, id_b)


# Site 3: line ~206 — get co-occurrence relationships for causality
BEFORE:
    rows = self._storage._conn.execute(
        "SELECT * FROM relationships "
        "WHERE relationship_type = 'co_occurrence' AND weight >= ?",
        (threshold,)
    ).fetchall()
AFTER:
    rows = self._storage.get_relationships_by_weight(
        min_weight=threshold, relationship_type="co_occurrence"
    )


# Site 4: line ~406 — get entity by ID in subgraph
BEFORE:
    row = self._storage._conn.execute(
        "SELECT * FROM entities WHERE id = ?", (eid,)
    ).fetchone()
    entity = dict(row) if row else None
AFTER:
    entity = self._storage.get_entity_by_id(eid)


# Site 5: line ~445 — get entity after insert
BEFORE:
    row = self._storage._conn.execute(
        "SELECT * FROM entities WHERE id = ?", (eid,)
    ).fetchone()
    entity = dict(row) if row else None
AFTER:
    entity = self._storage.get_entity_by_id(eid)


# Site 6: line ~453 — get typed relationship
BEFORE:
    row = self._storage._conn.execute(
        "SELECT * FROM relationships "
        "WHERE source_entity_id = ? AND target_entity_id = ? "
        "AND relationship_type = ?",
        (src_id, tgt_id, rel_type)
    ).fetchone()
AFTER:
    rel = self._storage.get_typed_relationship(src_id, tgt_id, rel_type)


# Sites 7-8: lines ~504, ~514 — get adjacent relationships
BEFORE:
    # With type filter:
    rows = self._storage._conn.execute(
        "SELECT r.*, e1.name ..., e2.name ... "
        "WHERE (src=? OR tgt=?) AND relationship_type IN (...)",
        params
    ).fetchall()
    # Without type filter:
    rows = self._storage._conn.execute(
        "SELECT r.*, e1.name ..., e2.name ... "
        "WHERE src=? OR tgt=?",
        (eid, eid)
    ).fetchall()
AFTER:
    # With type filter:
    rows = self._storage.get_adjacent_relationships(eid, relationship_types=[...])
    # Without:
    rows = self._storage.get_adjacent_relationships(eid)


# Site 9: line ~556 — get all episodes for temporal ordering
BEFORE:
    rows = self._storage._conn.execute(
        "SELECT raw_content FROM episodes ORDER BY timestamp ASC"
    ).fetchall()
    for row in rows:
        if entity_a in row[0]: found_a = True
AFTER:
    episodes = self._storage.get_all_episode_contents()
    for ep in episodes:
        if entity_a in ep["raw_content"]: found_a = True


# Site 10: line ~569 — get all hot memories for temporal ordering
BEFORE:
    rows = self._storage._conn.execute(
        "SELECT content FROM memories WHERE heat > 0 ORDER BY created_at ASC"
    ).fetchall()
    for row in rows:
        if entity_a in row[0]: found_a = True
AFTER:
    memories = self._storage.get_hot_memories_all()
    memories.sort(key=lambda m: m.get("created_at", ""))
    for mem in memories:
        if entity_a in mem["content"]: found_a = True
```

---

## 2f. cls_store.py (7 sites)

```
# Sites 1-2: lines ~119, ~126 — get episodic memories with embeddings
BEFORE:
    if directory:
        rows = self._storage._conn.execute(
            "SELECT * FROM memories WHERE store_type = 'episodic' "
            "AND heat > 0 AND embedding IS NOT NULL AND directory_context = ?",
            (directory,)
        ).fetchall()
    else:
        rows = self._storage._conn.execute(
            "SELECT * FROM memories WHERE store_type = 'episodic' "
            "AND heat > 0 AND embedding IS NOT NULL"
        ).fetchall()
    memories = self._storage._rows_to_dicts(rows)
AFTER:
    memories = self._storage.get_memories_by_store_type(
        "episodic", directory=directory if directory else None,
        min_heat=0.0, require_embedding=True
    )


# Site 3: line ~171 — get session_id from episode
BEFORE:
    row = self._storage._conn.execute(
        "SELECT session_id FROM episodes WHERE id = ?", (ep_id,)
    ).fetchone()
    session_id = row[0] if row else None
AFTER:
    session_id = self._storage.get_episode_session_id(ep_id)


# Sites 4-5: lines ~408, ~411 — count episodic/semantic memories
BEFORE:
    ep_count = self._storage._conn.execute(
        "SELECT COUNT(*) FROM memories WHERE store_type = 'episodic' AND heat > 0"
    ).fetchone()[0]
    sem_count = self._storage._conn.execute(
        "SELECT COUNT(*) FROM memories WHERE store_type = 'semantic' AND heat > 0"
    ).fetchone()[0]
AFTER:
    ep_count = self._storage.count_memories(store_type="episodic", min_heat=0.0)
    sem_count = self._storage.count_memories(store_type="semantic", min_heat=0.0)


# Sites 6-7: lines ~511, ~518 — search store by embedding
BEFORE:
    if directory:
        rows = self._storage._conn.execute(
            "SELECT * FROM memories WHERE store_type = ? "
            "AND heat > 0 AND embedding IS NOT NULL AND directory_context = ?",
            (store_type, directory)
        ).fetchall()
    else:
        rows = self._storage._conn.execute(...)
    memories = self._storage._rows_to_dicts(rows)
AFTER:
    memories = self._storage.get_memories_by_store_type(
        store_type, directory=directory if directory else None,
        min_heat=0.0, require_embedding=True
    )
```

---

## 2g. sleep_compute.py (5 sites)

```
# Site 1: line ~225 — get relationships for community graph
BEFORE:
    rows = self._storage._conn.execute(
        "SELECT source_entity_id, target_entity_id, weight FROM relationships"
    ).fetchall()
AFTER:
    rels = self._storage.get_all_relationships_for_graph()
    # Access: rel["source_entity_id"], rel["target_entity_id"], rel["weight"]


# Site 2: line ~295 — find memories mentioning entity names
BEFORE:
    rows = self._storage._conn.execute(
        "SELECT id, content FROM memories WHERE heat > 0"
    ).fetchall()
    # Then Python-side: if entity_name in row["content"]
AFTER:
    memories = self._storage.get_hot_memories_all()
    # Same Python-side: if entity_name in mem["content"]


# Site 3: line ~319 — get cluster members for summary
BEFORE:
    rows = self._storage._conn.execute(
        "SELECT id, content, embedding, heat FROM memories "
        "WHERE cluster_id = ? AND heat > 0",
        (cluster_id,)
    ).fetchall()
AFTER:
    members = self._storage.get_memories_in_cluster(cluster_id, min_heat=0.0)


# Site 4: line ~379 — get dominant directory for cluster
BEFORE:
    row = self._storage._conn.execute(
        "SELECT directory_context, COUNT(*) as cnt FROM memories "
        "WHERE cluster_id = ? GROUP BY directory_context "
        "ORDER BY cnt DESC LIMIT 1",
        (cluster_id,)
    ).fetchone()
    dir_ctx = row[0] if row else None
AFTER:
    dir_ctx = self._storage.get_cluster_dominant_directory(cluster_id)


# Site 5: line ~443 — get old verbose memories for compression
BEFORE:
    rows = self._storage._conn.execute(
        "SELECT id, content FROM memories "
        "WHERE created_at < ? AND LENGTH(content) > 1000 "
        "AND (compressed = 0 OR compressed IS NULL)",
        (cutoff_time,)
    ).fetchall()
AFTER:
    memories = self._storage.get_old_verbose_memories(cutoff_time, min_length=1000)
```

---

## 2h. metacognition.py (5 sites)

```
# Site 1: line ~221 — get all hot memories
BEFORE:
    rows = self._storage._conn.execute(
        "SELECT * FROM memories WHERE heat > 0"
    ).fetchall()
    memories = [dict(r) for r in rows]
AFTER:
    memories = self._storage.get_hot_memories_all()


# Site 2: line ~294 — check if relationship exists
BEFORE:
    row = self._storage._conn.execute(
        "SELECT id FROM relationships "
        "WHERE (source_entity_id = ? AND target_entity_id = ?) "
        "OR (source_entity_id = ? AND target_entity_id = ?)",
        (id_a, id_b, id_b, id_a)
    ).fetchone()
    if row is None:  # gap found
AFTER:
    if not self._storage.relationship_exists(id_a, id_b):
        # gap found


# Sites 3-4: lines ~301, ~304 — get entity names
BEFORE:
    name_a = self._storage._conn.execute(
        "SELECT name FROM entities WHERE id = ?", (id_a,)
    ).fetchone()
    name_b = self._storage._conn.execute(
        "SELECT name FROM entities WHERE id = ?", (id_b,)
    ).fetchone()
    # access: name_a[0], name_b[0]
AFTER:
    name_a = self._storage.get_entity_name(id_a)
    name_b = self._storage.get_entity_name(id_b)
    # direct string values (or None)


# Site 5: line ~334 — check if error has resolution
BEFORE:
    row = self._storage._conn.execute(
        "SELECT id FROM relationships "
        "WHERE source_entity_id = ? AND relationship_type = 'resolved_by'",
        (error_entity_id,)
    ).fetchone()
    if row is None:  # unresolved
AFTER:
    resolutions = self._storage.get_relationships_by_type_for_entity(
        error_entity_id, "resolved_by"
    )
    if not resolutions:  # unresolved
```

---

## 2i. rules_engine.py (4 sites)

```
# Site 1: line ~191 — get directory-scoped rules
BEFORE:
    rows = self._storage._conn.execute(
        "SELECT * FROM memory_rules WHERE scope = 'directory' "
        "AND is_active = 1 ORDER BY priority DESC"
    ).fetchall()
AFTER:
    rules = self._storage.get_rules_for_scope("directory")
    # Uses existing method


# Site 2: line ~203 — get file-scoped rules
BEFORE:
    rows = self._storage._conn.execute(
        "SELECT * FROM memory_rules WHERE scope = 'file' "
        "AND is_active = 1 ORDER BY priority DESC"
    ).fetchall()
AFTER:
    rules = self._storage.get_rules_for_scope("file")


# Site 3: line ~355 — check if rule exists
BEFORE:
    row = self._storage._conn.execute(
        "SELECT id FROM memory_rules WHERE id = ?", (rule_id,)
    ).fetchone()
    if row is None: return error
AFTER:
    if not self._storage.rule_exists(rule_id):
        return error


# Site 4: line ~365 — get all active rules sorted
BEFORE:
    rows = self._storage._conn.execute(
        "SELECT * FROM memory_rules WHERE is_active = 1 "
        "ORDER BY scope, priority DESC"
    ).fetchall()
AFTER:
    rules = self._storage.get_all_rules_sorted()
```

---

## 2j. predictive_coding.py (2 sites)

```
# Site 1: line ~270 — find most recent memory date mentioning entity
BEFORE:
    rows = self._storage._conn.execute(
        "SELECT m.created_at FROM memories m "
        "WHERE m.content LIKE ? AND m.heat > 0 "
        "ORDER BY m.created_at DESC LIMIT 1",
        (f"%{name}%",)
    ).fetchall()
    if rows:
        last_seen = rows[0][0]
AFTER:
    last_seen = self._storage.get_latest_memory_date_mentioning(name)
    # Returns str | None directly


# Site 2: line ~326 — get distinct relationship types
BEFORE:
    rows = self._storage._conn.execute(
        "SELECT DISTINCT r.relationship_type FROM relationships r"
    ).fetchall()
    rel_types = {row[0] for row in rows}
AFTER:
    rel_types = set(self._storage.get_distinct_relationship_types())
```

---

## 2k. causal_discovery.py (3 sites)

```
# Site 1: line ~64 — get episodes since time
BEFORE:
    rows = self._storage._conn.execute(
        "SELECT id, timestamp, directory, raw_content FROM episodes "
        "WHERE timestamp >= ? ORDER BY timestamp ASC",
        (since,)
    ).fetchall()
AFTER:
    episodes = self._storage.get_episodes_since_time(since)
    # Access: ep["id"], ep["timestamp"], ep["directory"], ep["raw_content"]


# Site 2: line ~486 — get causal causes
BEFORE:
    rows = self._storage._conn.execute(
        "SELECT cde.*, e.name AS source_name "
        "FROM causal_dag_edges cde "
        "JOIN entities e ON e.id = cde.source_entity_id "
        "WHERE cde.target_entity_id = ?",
        (entity_id,)
    ).fetchall()
AFTER:
    causes = self._storage.get_causal_causes(entity_id)


# Site 3: line ~536 — get causal effects
BEFORE:
    rows = self._storage._conn.execute(
        "SELECT cde.*, e.name AS target_name "
        "FROM causal_dag_edges cde "
        "JOIN entities e ON e.id = cde.target_entity_id "
        "WHERE cde.source_entity_id = ?",
        (entity_id,)
    ).fetchall()
AFTER:
    effects = self._storage.get_causal_effects(entity_id)
```

---

## 2l. narrative.py (1 site)

```
# Site 1: line ~222 — get active directories
BEFORE:
    rows = self._storage._conn.execute(
        "SELECT DISTINCT directory_context FROM memories WHERE heat >= ?",
        (min_heat,)
    ).fetchall()
    directories = [row[0] for row in rows if row[0]]
AFTER:
    directories = self._storage.get_active_directories(min_heat)
```

---

## 2m. restoration.py (2 sites)

```
# Site 1: line ~232 — get anchored memories
BEFORE:
    rows = self._storage._conn.execute(
        "SELECT * FROM memories WHERE is_protected = 1 AND heat > 0 "
        "AND tags LIKE '%_anchor%' ORDER BY created_at DESC LIMIT ?",
        (limit,)
    ).fetchall()
    anchored = [dict(r) for r in rows]
AFTER:
    anchored = self._storage.get_anchored_memories(min_heat=0.0, limit=limit)
    # BONUS: fixes L26 — the _anchor LIKE wildcard bug.
    # New method uses '%"_anchor"%' with quotes.


# Site 2: line ~254 — get recent non-anchored memories
BEFORE:
    rows = self._storage._conn.execute(
        "SELECT * FROM memories WHERE heat > 0 AND is_protected = 0 "
        "AND tags NOT LIKE '%_anchor%' ORDER BY created_at DESC LIMIT ?",
        (limit,)
    ).fetchall()
    recent = [dict(r) for r in rows]
AFTER:
    recent = self._storage.get_recent_memories(
        exclude_anchored=True, min_heat=0.0, limit=limit
    )
```

---

## 2n. seed.py (1 site)

```
# Site 1: line ~666 — find seed-tagged memories
BEFORE:
    rows = self._storage._conn.execute(
        'SELECT id FROM memories WHERE tags LIKE \'%"_seed"%\''
    ).fetchall()
    ids = [row[0] for row in rows]
AFTER:
    ids = self._storage.get_memory_ids_by_tag("_seed")
```

---

## 2o. astrocyte_pool.py (1 site)

```
# Site 1: line ~103 — get astrocyte process by ID after insert
BEFORE:
    proc = self._storage._conn.execute(
        "SELECT * FROM astrocyte_processes WHERE id = ?", (proc_id,)
    ).fetchone()
    proc = self._storage._row_to_dict(proc)
AFTER:
    proc = self._storage.get_astrocyte_process(proc_id)
```

---

## 2p. fractal.py (3 sites)

```
# Site 1: line ~399 — get child clusters
BEFORE:
    rows = self._storage._conn.execute(
        "SELECT * FROM memory_clusters WHERE parent_cluster_id = ? "
        "ORDER BY heat DESC",
        (parent_id,)
    ).fetchall()
AFTER:
    clusters = self._storage.get_child_clusters(parent_id)


# Site 2: line ~418 — get memories in cluster
BEFORE:
    rows = self._storage._conn.execute(
        "SELECT * FROM memories WHERE cluster_id = ? AND heat > 0 "
        "ORDER BY heat DESC",
        (cluster_id,)
    ).fetchall()
AFTER:
    memories = self._storage.get_memories_in_cluster(cluster_id, min_heat=0.0)


# Site 3: line ~534 — get cluster member IDs
BEFORE:
    rows = self._storage._conn.execute(
        "SELECT id FROM memories WHERE cluster_id = ? AND heat > 0",
        (cluster_id,)
    ).fetchall()
    member_ids = [row[0] for row in rows]
AFTER:
    member_ids = self._storage.get_cluster_member_ids(cluster_id, min_heat=0.0)
```

---

## 2q. write_queue.py (3 sites)

```
# Site 1: line ~195 — BEGIN transaction
BEFORE:
    self._storage._conn.execute("BEGIN")
AFTER:
    conn = self._storage.get_write_connection()
    conn.execute("BEGIN")


# Site 2: line ~199 — COMMIT
BEFORE:
    self._storage._conn.commit()
AFTER:
    conn.commit()


# Site 3: line ~202 — ROLLBACK on exception
BEFORE:
    self._storage._conn.rollback()
AFTER:
    conn.rollback()

# Note: conn is obtained once at the start of _execute_batch and reused
# for all three operations within the same method scope.
```
