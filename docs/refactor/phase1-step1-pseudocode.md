# Step 1: New Domain Methods for StorageEngine

All methods added to `vaire/storage.py` inside the `StorageEngine` class.
All use `self.__conn` (the thread-local connection property).
All READ methods — no commits needed.
All use strict `heat > ?` to match existing codebase semantics.

---

## 1a. Memory counting & stats

```
METHOD count_memories(store_type=None, min_heat=0.0, compression_level=None) -> int:
    sql = "SELECT COUNT(*) FROM memories WHERE heat > ?"
    params = [min_heat]
    
    IF store_type is not None:
        sql += " AND store_type = ?"
        params.append(store_type)
    
    IF compression_level is not None:
        sql += " AND compression_level = ?"
        params.append(compression_level)
    
    row = self.__conn.execute(sql, params).fetchone()
    RETURN row[0] if row else 0


METHOD sum_reconsolidation_count() -> int:
    row = self.__conn.execute(
        "SELECT COALESCE(SUM(reconsolidation_count), 0) FROM memories"
    ).fetchone()
    RETURN row[0] if row else 0


METHOD count_causal_relationships() -> int:
    row = self.__conn.execute(
        "SELECT COUNT(*) FROM relationships WHERE is_causal = 1"
    ).fetchone()
    RETURN row[0] if row else 0
```

---

## 1b. Specialized memory queries

```
METHOD get_memories_by_store_type(store_type, directory=None, min_heat=0.0,
                                  require_embedding=False, limit=None) -> list[dict]:
    sql = "SELECT * FROM memories WHERE store_type = ? AND heat > ?"
    params = [store_type, min_heat]
    
    IF require_embedding:
        sql += " AND embedding IS NOT NULL"
    
    IF directory is not None:
        sql += " AND directory_context = ?"
        params.append(directory)
    
    IF limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    
    rows = self.__conn.execute(sql, params).fetchall()
    RETURN self._rows_to_dicts(rows)


METHOD get_anchored_memories(min_heat=0.0, limit=50) -> list[dict]:
    # Uses '"_anchor"' with quotes to avoid SQL _ wildcard bug (fixes L26)
    rows = self.__conn.execute(
        'SELECT * FROM memories WHERE is_protected = 1 AND heat > ? '
        'AND tags LIKE \'%"_anchor"%\' '
        'ORDER BY created_at DESC LIMIT ?',
        (min_heat, limit)
    ).fetchall()
    RETURN self._rows_to_dicts(rows)


METHOD get_recent_memories(exclude_anchored=True, min_heat=0.0, limit=50) -> list[dict]:
    sql = "SELECT * FROM memories WHERE heat > ?"
    params = [min_heat]
    
    IF exclude_anchored:
        sql += ' AND is_protected = 0 AND tags NOT LIKE \'%"_anchor"%\''
    
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    
    rows = self.__conn.execute(sql, params).fetchall()
    RETURN self._rows_to_dicts(rows)


METHOD get_memories_for_pruning() -> list[dict]:
    rows = self.__conn.execute(
        "SELECT * FROM memories WHERE heat < 0.01 AND confidence < 0.3 "
        "AND access_count = 0 AND store_type != 'reference'"
    ).fetchall()
    RETURN self._rows_to_dicts(rows)


METHOD get_memories_for_strengthening() -> list[dict]:
    rows = self.__conn.execute(
        "SELECT id, importance FROM memories "
        "WHERE access_count > 5 AND confidence > 0.8 AND importance < 1.0 "
        "AND store_type != 'reference'"
    ).fetchall()
    RETURN self._rows_to_dicts(rows)


METHOD get_memories_in_cluster(cluster_id, min_heat=0.0) -> list[dict]:
    rows = self.__conn.execute(
        "SELECT * FROM memories WHERE cluster_id = ? AND heat > ? "
        "ORDER BY heat DESC",
        (cluster_id, min_heat)
    ).fetchall()
    RETURN self._rows_to_dicts(rows)


METHOD get_old_verbose_memories(before, min_length=1000) -> list[dict]:
    rows = self.__conn.execute(
        "SELECT id, content FROM memories "
        "WHERE created_at < ? AND LENGTH(content) > ? "
        "AND (compressed = 0 OR compressed IS NULL)",
        (before, min_length)
    ).fetchall()
    RETURN self._rows_to_dicts(rows)


METHOD get_memory_ids_by_heat(min_heat, limit) -> list[int]:
    rows = self.__conn.execute(
        "SELECT id FROM memories WHERE heat > ? LIMIT ?",
        (min_heat, limit)
    ).fetchall()
    RETURN [row[0] for row in rows]


METHOD get_hdc_vector(memory_id) -> bytes | None:
    row = self.__conn.execute(
        "SELECT hdc_vector FROM memories WHERE id = ?",
        (memory_id,)
    ).fetchone()
    RETURN row[0] if row else None


METHOD find_memories_mentioning(text, min_heat=0.0) -> list[int]:
    # Try FTS5 first
    TRY:
        fts_query = self._preprocess_fts_query(text)
        rows = self.__conn.execute(
            "SELECT m.id FROM memories m "
            "JOIN memories_fts fts ON m.id = fts.rowid "
            "WHERE memories_fts MATCH ? AND m.heat > ?",
            (fts_query, min_heat)
        ).fetchall()
        IF rows:
            RETURN [r[0] for r in rows]
    EXCEPT Exception:
        pass  # fall through to LIKE
    
    # LIKE fallback — escape % and _ in user text
    escaped = text.replace("%", "\\%").replace("_", "\\_")
    rows = self.__conn.execute(
        "SELECT id FROM memories WHERE content LIKE ? ESCAPE '\\' AND heat > ?",
        (f"%{escaped}%", min_heat)
    ).fetchall()
    RETURN [r[0] for r in rows]


METHOD get_hot_memories_all(min_heat=0.0) -> list[dict]:
    rows = self.__conn.execute(
        "SELECT * FROM memories WHERE heat > ?",
        (min_heat,)
    ).fetchall()
    RETURN self._rows_to_dicts(rows)


METHOD memory_exists_with_content(content) -> bool:
    row = self.__conn.execute(
        "SELECT 1 FROM memories WHERE content = ? LIMIT 1",
        (content,)
    ).fetchone()
    RETURN row is not None


METHOD get_latest_memory_date_mentioning(text) -> str | None:
    # Escape LIKE wildcards in user text
    escaped = text.replace("%", "\\%").replace("_", "\\_")
    row = self.__conn.execute(
        "SELECT created_at FROM memories "
        "WHERE content LIKE ? ESCAPE '\\' AND heat > 0 "
        "ORDER BY created_at DESC LIMIT 1",
        (f"%{escaped}%",)
    ).fetchone()
    RETURN row[0] if row else None
```

---

## 1c. Entity queries

```
METHOD get_entity_name(entity_id) -> str | None:
    row = self.__conn.execute(
        "SELECT name FROM entities WHERE id = ?",
        (entity_id,)
    ).fetchone()
    RETURN row[0] if row else None


METHOD get_entity_heat(entity_id) -> float | None:
    row = self.__conn.execute(
        "SELECT heat FROM entities WHERE id = ?",
        (entity_id,)
    ).fetchone()
    RETURN row[0] if row else None


METHOD get_entity_by_id(entity_id) -> dict | None:
    row = self.__conn.execute(
        "SELECT * FROM entities WHERE id = ?",
        (entity_id,)
    ).fetchone()
    RETURN self._row_to_dict(row)
```

---

## 1d. Relationship queries

```
METHOD get_relationships_by_weight(min_weight, relationship_type=None) -> list[dict]:
    sql = "SELECT * FROM relationships WHERE weight >= ?"
    params = [min_weight]
    
    IF relationship_type is not None:
        sql += " AND relationship_type = ?"
        params.append(relationship_type)
    
    rows = self.__conn.execute(sql, params).fetchall()
    RETURN self._rows_to_dicts(rows)


METHOD get_relationships_at_time(entity_id, before_time) -> list[dict]:
    rows = self.__conn.execute(
        "SELECT r.*, e1.name AS source_name, e2.name AS target_name "
        "FROM relationships r "
        "JOIN entities e1 ON e1.id = r.source_entity_id "
        "JOIN entities e2 ON e2.id = r.target_entity_id "
        "WHERE (r.source_entity_id = ? OR r.target_entity_id = ?) "
        "AND r.event_time <= ?",
        (entity_id, entity_id, before_time)
    ).fetchall()
    RETURN self._rows_to_dicts(rows)


METHOD get_relationship_history(entity_id_a, entity_id_b) -> list[dict]:
    rows = self.__conn.execute(
        "SELECT r.*, e1.name AS source_name, e2.name AS target_name "
        "FROM relationships r "
        "JOIN entities e1 ON e1.id = r.source_entity_id "
        "JOIN entities e2 ON e2.id = r.target_entity_id "
        "WHERE (r.source_entity_id = ? AND r.target_entity_id = ?) "
        "OR (r.source_entity_id = ? AND r.target_entity_id = ?)",
        (entity_id_a, entity_id_b, entity_id_b, entity_id_a)
    ).fetchall()
    RETURN self._rows_to_dicts(rows)


METHOD get_adjacent_relationships(entity_id, relationship_types=None) -> list[dict]:
    IF relationship_types:
        placeholders = ",".join("?" for _ in relationship_types)
        sql = (
            "SELECT r.*, e1.name AS source_name, e2.name AS target_name "
            "FROM relationships r "
            "JOIN entities e1 ON e1.id = r.source_entity_id "
            "JOIN entities e2 ON e2.id = r.target_entity_id "
            "WHERE (r.source_entity_id = ? OR r.target_entity_id = ?) "
            f"AND r.relationship_type IN ({placeholders})"
        )
        params = [entity_id, entity_id] + relationship_types
    ELSE:
        sql = (
            "SELECT r.*, e1.name AS source_name, e2.name AS target_name "
            "FROM relationships r "
            "JOIN entities e1 ON e1.id = r.source_entity_id "
            "JOIN entities e2 ON e2.id = r.target_entity_id "
            "WHERE r.source_entity_id = ? OR r.target_entity_id = ?"
        )
        params = [entity_id, entity_id]
    
    rows = self.__conn.execute(sql, params).fetchall()
    RETURN self._rows_to_dicts(rows)


METHOD get_all_relationships_for_graph() -> list[dict]:
    # Lightweight: only IDs and weight for graph building
    rows = self.__conn.execute(
        "SELECT source_entity_id, target_entity_id, weight "
        "FROM relationships"
    ).fetchall()
    RETURN [dict(r) for r in rows]


METHOD relationship_exists(entity_id_a, entity_id_b) -> bool:
    row = self.__conn.execute(
        "SELECT 1 FROM relationships "
        "WHERE (source_entity_id = ? AND target_entity_id = ?) "
        "OR (source_entity_id = ? AND target_entity_id = ?) LIMIT 1",
        (entity_id_a, entity_id_b, entity_id_b, entity_id_a)
    ).fetchone()
    RETURN row is not None


METHOD get_relationships_by_type_for_entity(entity_id, relationship_type) -> list[dict]:
    rows = self.__conn.execute(
        "SELECT * FROM relationships "
        "WHERE source_entity_id = ? AND relationship_type = ?",
        (entity_id, relationship_type)
    ).fetchall()
    RETURN self._rows_to_dicts(rows)


METHOD get_distinct_relationship_types() -> list[str]:
    rows = self.__conn.execute(
        "SELECT DISTINCT relationship_type FROM relationships"
    ).fetchall()
    RETURN [row[0] for row in rows]


METHOD get_typed_relationship(source_id, target_id, relationship_type) -> dict | None:
    row = self.__conn.execute(
        "SELECT * FROM relationships "
        "WHERE source_entity_id = ? AND target_entity_id = ? "
        "AND relationship_type = ?",
        (source_id, target_id, relationship_type)
    ).fetchone()
    RETURN self._row_to_dict(row)
```

---

## 1e. Episode queries

```
METHOD get_all_episode_contents() -> list[dict]:
    rows = self.__conn.execute(
        "SELECT id, raw_content, timestamp FROM episodes ORDER BY timestamp ASC"
    ).fetchall()
    RETURN [dict(r) for r in rows]


METHOD get_episodes_since_time(since) -> list[dict]:
    rows = self.__conn.execute(
        "SELECT * FROM episodes WHERE timestamp >= ? ORDER BY timestamp ASC",
        (since,)
    ).fetchall()
    RETURN self._rows_to_dicts(rows)


METHOD get_episode_session_id(episode_id) -> str | None:
    row = self.__conn.execute(
        "SELECT session_id FROM episodes WHERE id = ?",
        (episode_id,)
    ).fetchone()
    RETURN row[0] if row else None
```

---

## 1f. Causal DAG queries

```
METHOD get_causal_causes(entity_id) -> list[dict]:
    rows = self.__conn.execute(
        "SELECT cde.*, e.name AS source_name "
        "FROM causal_dag_edges cde "
        "JOIN entities e ON e.id = cde.source_entity_id "
        "WHERE cde.target_entity_id = ?",
        (entity_id,)
    ).fetchall()
    RETURN self._rows_to_dicts(rows)


METHOD get_causal_effects(entity_id) -> list[dict]:
    rows = self.__conn.execute(
        "SELECT cde.*, e.name AS target_name "
        "FROM causal_dag_edges cde "
        "JOIN entities e ON e.id = cde.target_entity_id "
        "WHERE cde.source_entity_id = ?",
        (entity_id,)
    ).fetchall()
    RETURN self._rows_to_dicts(rows)


METHOD count_causal_relationships() -> int:
    row = self.__conn.execute(
        "SELECT COUNT(*) FROM relationships WHERE is_causal = 1"
    ).fetchone()
    RETURN row[0] if row else 0
```

---

## 1g. Cluster queries

```
METHOD get_child_clusters(parent_cluster_id) -> list[dict]:
    rows = self.__conn.execute(
        "SELECT * FROM memory_clusters WHERE parent_cluster_id = ? "
        "ORDER BY heat DESC",
        (parent_cluster_id,)
    ).fetchall()
    RETURN self._rows_to_dicts(rows)


METHOD get_cluster_dominant_directory(cluster_id) -> str | None:
    row = self.__conn.execute(
        "SELECT directory_context FROM memories "
        "WHERE cluster_id = ? "
        "GROUP BY directory_context ORDER BY COUNT(*) DESC LIMIT 1",
        (cluster_id,)
    ).fetchone()
    RETURN row[0] if row else None


METHOD get_cluster_member_ids(cluster_id, min_heat=0.0) -> list[int]:
    rows = self.__conn.execute(
        "SELECT id FROM memories WHERE cluster_id = ? AND heat > ?",
        (cluster_id, min_heat)
    ).fetchall()
    RETURN [row[0] for row in rows]
```

---

## 1h. Action log, metadata, directory, rule, seed, astrocyte queries

```
METHOD get_unprocessed_action_log(limit=200) -> list[dict]:
    rows = self.__conn.execute(
        "SELECT id, tool_name, tool_input_summary, directory, timestamp "
        "FROM action_log WHERE processed = 0 "
        "ORDER BY timestamp ASC LIMIT ?",
        (limit,)
    ).fetchall()
    RETURN [dict(r) for r in rows]


METHOD get_metadata_value(key) -> str | None:
    row = self.__conn.execute(
        "SELECT value FROM metadata WHERE key = ?",
        (key,)
    ).fetchone()
    RETURN row[0] if row else None


METHOD get_active_directories(min_heat=0.0) -> list[str]:
    rows = self.__conn.execute(
        "SELECT DISTINCT directory_context FROM memories WHERE heat > ?",
        (min_heat,)
    ).fetchall()
    RETURN [row[0] for row in rows if row[0]]


METHOD rule_exists(rule_id) -> bool:
    row = self.__conn.execute(
        "SELECT 1 FROM memory_rules WHERE id = ?",
        (rule_id,)
    ).fetchone()
    RETURN row is not None


METHOD get_all_rules_sorted() -> list[dict]:
    rows = self.__conn.execute(
        "SELECT * FROM memory_rules WHERE is_active = 1 "
        "ORDER BY scope, priority DESC"
    ).fetchall()
    RETURN self._rows_to_dicts(rows)


METHOD get_memory_ids_by_tag(tag) -> list[int]:
    # Proper JSON-aware check: tag is inside a JSON array
    rows = self.__conn.execute(
        'SELECT id FROM memories WHERE tags LIKE ?',
        (f'%"{tag}"%',)
    ).fetchall()
    RETURN [row[0] for row in rows]


METHOD get_astrocyte_process(process_id) -> dict | None:
    row = self.__conn.execute(
        "SELECT * FROM astrocyte_processes WHERE id = ?",
        (process_id,)
    ).fetchone()
    RETURN self._row_to_dict(row)
```

---

## 1i. write_queue transaction support

```
METHOD get_write_connection(self):
    """Sanctioned escape hatch for WriteQueue transaction management.
    Returns the thread-local connection for BEGIN/COMMIT/ROLLBACK.
    Only WriteQueue should call this."""
    RETURN self.__conn
```

---

## Complete method count: 38 new methods

| Category | Methods | Count |
|---|---|---|
| Memory counting | count_memories, sum_reconsolidation_count | 2 |
| Memory queries | get_memories_by_store_type, get_anchored_memories, get_recent_memories, get_memories_for_pruning, get_memories_for_strengthening, get_memories_in_cluster, get_old_verbose_memories, get_memory_ids_by_heat, get_hdc_vector, find_memories_mentioning, get_hot_memories_all, memory_exists_with_content, get_latest_memory_date_mentioning | 13 |
| Entity queries | get_entity_name, get_entity_heat, get_entity_by_id | 3 |
| Relationship queries | get_relationships_by_weight, get_relationships_at_time, get_relationship_history, get_adjacent_relationships, get_all_relationships_for_graph, relationship_exists, get_relationships_by_type_for_entity, get_distinct_relationship_types, get_typed_relationship | 9 |
| Episode queries | get_all_episode_contents, get_episodes_since_time, get_episode_session_id | 3 |
| Causal queries | get_causal_causes, get_causal_effects, count_causal_relationships | 3 |
| Cluster queries | get_child_clusters, get_cluster_dominant_directory, get_cluster_member_ids | 3 |
| Misc queries | get_unprocessed_action_log, get_metadata_value, get_active_directories, rule_exists, get_all_rules_sorted, get_memory_ids_by_tag, get_astrocyte_process, get_write_connection | 8 |
| **Total** | | **38** |
