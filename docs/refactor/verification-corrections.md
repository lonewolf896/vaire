# Verification Corrections

All corrections identified during logic verification of Phases 1-4 pseudocode.
Apply these before implementation.

---

## Phase 1 Corrections

### CRITICAL: rules_engine.py migration uses wrong existing method

**Problem:** The pseudocode calls `self._storage.get_rules_for_scope("directory")` but
the existing method adds `AND scope_value = ?` for non-global scopes. Passing no
scope_value filters to `scope_value IS NULL`, silently dropping all directory rules
that have a scope_value. The original code intentionally fetches ALL directory rules
then does Python-side prefix matching.

**Fix:** Do NOT use the existing `get_rules_for_scope()` for rules_engine.py sites 1-2.
Instead, add a new method:

```
METHOD get_rules_by_scope_type(scope: str) -> list[dict]:
    """Get all active rules for a scope type (no scope_value filter).
    Used by RulesEngine which does its own prefix matching."""
    rows = self.__conn.execute(
        "SELECT * FROM memory_rules WHERE scope = ? AND is_active = 1 "
        "ORDER BY priority DESC",
        (scope,)
    ).fetchall()
    RETURN self._rows_to_dicts(rows)
```

Migration becomes:
```
# rules_engine.py Site 1:
rules = self._storage.get_rules_by_scope_type("directory")

# rules_engine.py Site 2:
rules = self._storage.get_rules_by_scope_type("file")
```

---

### HIGH: get_active_directories uses `heat > ?` but original uses `heat >= ?`

**Fix:** Change `get_active_directories` to use `heat >= ?`:

```
METHOD get_active_directories(min_heat=0.0) -> list[str]:
    rows = self.__conn.execute(
        "SELECT DISTINCT directory_context FROM memories WHERE heat >= ?",
        (min_heat,)
    ).fetchall()
    RETURN [row[0] for row in rows if row[0]]
```

This is the ONE exception to the "use `heat > ?` everywhere" rule, because the
original SQL in narrative.py explicitly uses `>=`.

---

### HIGH: Missing ORDER BY in get_relationships_at_time

**Fix:** Add `ORDER BY r.event_time DESC`:

```
METHOD get_relationships_at_time(entity_id, before_time) -> list[dict]:
    rows = self.__conn.execute(
        "SELECT r.*, e1.name AS source_name, e2.name AS target_name "
        "FROM relationships r "
        "JOIN entities e1 ON e1.id = r.source_entity_id "
        "JOIN entities e2 ON e2.id = r.target_entity_id "
        "WHERE (r.source_entity_id = ? OR r.target_entity_id = ?) "
        "AND r.event_time <= ? "
        "ORDER BY r.event_time DESC",
        (entity_id, entity_id, before_time)
    ).fetchall()
    RETURN self._rows_to_dicts(rows)
```

---

### HIGH: Missing ORDER BY in get_relationship_history

**Fix:** Add `ORDER BY r.created_at ASC`:

```
METHOD get_relationship_history(entity_id_a, entity_id_b) -> list[dict]:
    rows = self.__conn.execute(
        "SELECT r.*, e1.name AS source_name, e2.name AS target_name "
        "FROM relationships r "
        "JOIN entities e1 ON e1.id = r.source_entity_id "
        "JOIN entities e2 ON e2.id = r.target_entity_id "
        "WHERE (r.source_entity_id = ? AND r.target_entity_id = ?) "
        "OR (r.source_entity_id = ? AND r.target_entity_id = ?) "
        "ORDER BY r.created_at ASC",
        (entity_id_a, entity_id_b, entity_id_b, entity_id_a)
    ).fetchall()
    RETURN self._rows_to_dicts(rows)
```

---

### MEDIUM: get_entity_heat conflates "not found" with "heat is NULL"

**Problem:** Original code at curation.py:428 distinguishes `src is None` (entity not
found → skip entirely) from `src[0] is not None` (NULL heat → treat as 0.0).
The pseudocode migration uses `get_entity_heat(id) or 0.0` which treats both as 0.0.

**Fix:** Change `get_entity_heat` to return 0.0 for NULL heat (matching original behavior),
and add a separate None check for entity existence:

```
# In curation.py migration, Site 4:
BEFORE pseudocode:
    src_heat_val = self._storage.get_entity_heat(src_id)
    if src_heat_val is None: continue
    src_heat = src_heat_val

AFTER (corrected):
    entity = self._storage.get_entity_by_id(src_id)
    if entity is None: continue
    src_heat = entity.get("heat") or 0.0

# Same pattern for Site 5 (target entity):
    entity = self._storage.get_entity_by_id(tgt_id)
    if entity is None: continue
    tgt_heat = entity.get("heat") or 0.0
```

This preserves the original two-level check: entity existence (skip if missing)
and heat value (default to 0.0 if NULL).

---

### MEDIUM: get_relationships_by_weight(min_weight=0.0) excludes NULL-weight rows

**Problem:** Original code at curation.py:417 has no WHERE clause — fetches ALL
relationships including those with NULL weight. The method adds
`WHERE weight >= 0.0` which excludes NULL-weight rows.

**Fix:** For the `_memify_reweight` callsite, use a different method or add
NULL handling:

Option A — Change the domain method to handle NULL:
```
METHOD get_relationships_by_weight(min_weight, relationship_type=None) -> list[dict]:
    IF min_weight <= 0 and relationship_type is None:
        # Special case: fetch all (including NULL weight)
        sql = "SELECT * FROM relationships"
        params = []
    ELSE:
        sql = "SELECT * FROM relationships WHERE COALESCE(weight, 0) >= ?"
        params = [min_weight]
    
    IF relationship_type is not None:
        sql += (" AND " if "WHERE" in sql else " WHERE ")
        sql += "relationship_type = ?"
        params.append(relationship_type)
    
    rows = self.__conn.execute(sql, params).fetchall()
    RETURN self._rows_to_dicts(rows)
```

Option B (simpler) — Add a dedicated method for the reweight use case:
```
METHOD get_all_relationships() -> list[dict]:
    rows = self.__conn.execute("SELECT * FROM relationships").fetchall()
    RETURN self._rows_to_dicts(rows)
```

Going with **Option A** — `COALESCE(weight, 0)` treats NULL as 0, matching the
original behavior where NULL weight rows are included and defaulted to 1.0 in Python.

---

### MEDIUM: get_hot_memories_all missing ORDER BY for knowledge_graph temporal use

**Problem:** knowledge_graph.py:569 uses `ORDER BY created_at ASC` but
`get_hot_memories_all()` has no ORDER BY. The pseudocode migration adds a
Python-side sort which works but is less efficient.

**Fix:** This is acceptable as-is. The Python sort is correct and avoids adding
an `order_by` parameter to a general-purpose method. The caller site (knowledge_graph.py)
already sorts after the call. No change needed — just documenting the trade-off.

---

### LOW: count_causal_relationships() defined twice in step 1

**Fix:** Remove the duplicate from section 1f. Keep it only in section 1a.

---

### LOW: Incomplete access pattern documentation for sleep_compute.py

**Fix:** Document in phase1-step2-pseudocode.md that sleep_compute.py sites 2 and 3
must change tuple indexing to dict access. This is already implied by using
`get_hot_memories_all()` and `get_memories_in_cluster()` which return dicts,
but making it explicit prevents mistakes during implementation.

---

### LOW: Column selection differences

**Fix:** No code change needed. Using `SELECT *` is functionally correct.
The performance difference is negligible at DB size ~1000.

---

## Phase 3 Corrections

### HIGH: resolve_conflict() clock persistence/rollback mismatch

**Problem:** The pseudocode changes `resolve_conflict()` to use `self.increment_clock()`
which calls `_save_vector_clock()`. If the subsequent `update_memory_full()` fails,
the in-memory clock is rolled back (`self._vector_clock = clock_snapshot`) but the
persisted clock retains the incremented value.

**Fix:** Add `_save_vector_clock()` to the rollback path:

```
IN crdt_sync.py, resolve_conflict():

    clock_snapshot = dict(self._vector_clock)
    self.increment_clock()  # increments AND persists
    new_clock = json.dumps(dict(self._vector_clock))
    TRY:
        self._storage.update_memory_full(
            memory_id,
            content=resolved_content,
            vector_clock=new_clock,
        )
    EXCEPT Exception:
        self._vector_clock = clock_snapshot
        self._save_vector_clock()  # ADDED: persist the rollback too
        raise
```

---

## Phase 4 Corrections

### MEDIUM: _ENTITY_PATTERN_RE must not be deleted

**Problem:** `_ENTITY_PATTERN_RE` is used by `generate_cluster_summaries()` at line 330
of sleep_compute.py. The pseudocode says to check — the answer is: keep it.

**Fix:** When removing `compress_old_memories()`:
- DELETE `_SENTENCE_RE` (only used in compress_old_memories)
- KEEP `_ENTITY_PATTERN_RE` (also used in generate_cluster_summaries)

---

## Summary

| # | Phase | Severity | Fix |
|---|---|---|---|
| 1 | P1 | CRITICAL | Add `get_rules_by_scope_type()`, don't use `get_rules_for_scope()` |
| 2 | P1 | HIGH | `get_active_directories` uses `heat >= ?` (exception to > rule) |
| 3 | P1 | HIGH | Add `ORDER BY r.event_time DESC` to `get_relationships_at_time` |
| 4 | P1 | HIGH | Add `ORDER BY r.created_at ASC` to `get_relationship_history` |
| 5 | P1 | MEDIUM | Curation entity heat: use `get_entity_by_id` + null check |
| 6 | P1 | MEDIUM | `get_relationships_by_weight`: use `COALESCE(weight, 0)` for NULL |
| 7 | P1 | MEDIUM | `get_hot_memories_all` sort — acceptable as Python-side sort |
| 8 | P1 | LOW | Remove duplicate `count_causal_relationships` from section 1f |
| 9 | P1 | LOW | Document tuple→dict access changes for sleep_compute.py |
| 10 | P1 | LOW | SELECT * vs specific columns — no change needed |
| 11 | P3 | HIGH | Add `_save_vector_clock()` to resolve_conflict rollback path |
| 12 | P4 | MEDIUM | Keep `_ENTITY_PATTERN_RE`, only delete `_SENTENCE_RE` |
| 13 | P4 | MEDIUM | Update test_sleep_compute.py: remove tests calling compress_old_memories() (lines 336, 352, 367) |
| 14 | P4 | LOW | Consider using ACTION_LOG_INTERVAL (60s) instead of DAEMON_CHECK_INTERVAL (30s) for decay gap — more semantically precise |

## Second Verification Pass (Round 2)

All 12 original corrections re-verified against source code. **Clean pass — no errors in corrections.**
Items 13-14 added from round 2 findings.
