# Phase 2, Step 2: Add Explicit FTS Sync to All Write Paths

Without triggers, every method that INSERTs, UPDATEs content, or DELETEs from `memories`
must explicitly sync `memories_fts`. Methods that UPDATE non-content fields (heat, tags,
embedding, etc.) do NOT need FTS sync — the trigger was wrong to rewrite FTS on those.

This is the key insight: **only content changes need FTS sync**. The trigger was
over-broad — it fired on EVERY update, not just content changes.

---

## Categorizing all 19 UPDATE paths

### Category A: Updates content → NEEDS FTS sync (3 paths)
- Line ~695: enrichment UPDATE (sets enriched_content, enrichment_* fields)
  - NOTE: This updates enrichment columns, not `content` itself. 
  - The FTS should reflect the original content (enriched via _enrich_content_for_fts).
  - No FTS change needed here — FTS was already set at insert time.
  - The trigger was WRONG to fire here — it was reverting enrichment to raw.
- Line ~1883-1889: compression UPDATE (sets content, embedding, compression_level)
  - YES: content changes → FTS must be updated
- Line ~2211: upsert UPDATE path (sets content, embedding, heat, directory, tags)
  - YES: content changes → FTS already handled explicitly at line 2220-2223
- Line ~2796: update_memory_full (sets arbitrary fields including possibly content)
  - YES: content changes → FTS already handled explicitly at line 2800-2804

### Category B: Updates non-content fields → NO FTS sync needed (16 paths)
- Line ~388: SET content_fidelity = 'full' (no content change)
- Line ~706: SET embedding = ? (re-embed with enriched content, no content change)
- Line ~837: SET heat = ? (heat decay)
- Line ~843: SET is_stale = ? (staleness flag)
- Line ~1153: SET embedding = ?, embedding_model = ? (re-embedding)
- Line ~1583: SET {dynamic fields} (update_memory_scores — importance, surprise, etc.)
- Line ~1596: SET access_count, useful_count, confidence (usage stats)
- Line ~1763: SET sr_x, sr_y (successor representation coordinates)
- Line ~1842: SET slot_index, excitability (engram allocation)
- Line ~2264: SET heat, last_accessed (access update)
- Line ~2274: SET tags, provenance_agent (tag update)
- Line ~2383: SET heat (groomer heat update)
- Line ~2483: SET is_stale = 1 (archive)
- Line ~2827: SET heat, is_protected, tags (groomer approve)
- Line ~2839: SET heat, is_protected, tags (groomer reject)

**Key realization:** 16 of the 19 UPDATE paths don't change content at all.
The trigger was firing unnecessarily on all of them, resetting FTS each time.
Removing the trigger and only syncing FTS on actual content changes is strictly better.

---

## Paths that need explicit FTS sync after trigger removal

### Path 1: insert_memory() — line ~658-668

Currently: trigger auto-inserts raw content, then manual UPDATE enriches it.
After trigger removal: must INSERT into FTS explicitly.

```
BEFORE (with trigger):
    # Line 658: INSERT INTO memories ... → trigger auto-inserts into memories_fts
    cur = self.__conn.execute("INSERT INTO memories ...", params)
    self._guarded_commit()
    memory_id = cur.lastrowid
    # Line 662-668: manual enrichment overwrites trigger's raw content
    enriched = self._enrich_content_for_fts(content)
    if enriched != content:
        self.__conn.execute(
            "UPDATE memories_fts SET content = ? WHERE rowid = ?",
            (enriched, memory_id)
        )
        self._guarded_commit()

AFTER (no trigger):
    cur = self.__conn.execute("INSERT INTO memories ...", params)
    self._guarded_commit()
    memory_id = cur.lastrowid
    # Explicit FTS insert with enriched content
    enriched = self._enrich_content_for_fts(content)
    self.__conn.execute(
        "INSERT INTO memories_fts(rowid, content) VALUES (?, ?)",
        (memory_id, enriched)
    )
    self._guarded_commit()
    # No conditional — always insert into FTS, always enriched
```

### Path 2: delete_memory() / _delete_memory_inner() — line ~858-875

Currently: trigger auto-deletes from FTS on DELETE FROM memories.
After trigger removal: must DELETE from FTS explicitly.

```
BEFORE (with trigger):
    # _delete_memory_inner line 875:
    self.__conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    # → trigger auto-deletes from memories_fts

AFTER (no trigger):
    # Add explicit FTS delete BEFORE deleting the memory row
    self.__conn.execute(
        "DELETE FROM memories_fts WHERE rowid = ?", (memory_id,)
    )
    self.__conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
```

### Path 3: update_memory_compression() — line ~1883-1889

This method changes content (compressed version). FTS must be updated.

```
BEFORE (with trigger):
    self.__conn.execute(
        "UPDATE memories SET content = ?, embedding = ?, compression_level = ?, "
        "compressed = 1 WHERE id = ?",
        (content, embedding, compression_level, memory_id)
    )
    # → trigger overwrites FTS with raw compressed content
    # This is actually correct here since we WANT the compressed content in FTS

AFTER (no trigger):
    self.__conn.execute(
        "UPDATE memories SET content = ?, embedding = ?, compression_level = ?, "
        "compressed = 1 WHERE id = ?",
        (content, embedding, compression_level, memory_id)
    )
    # Explicit FTS update with enriched compressed content
    enriched = self._enrich_content_for_fts(content)
    self.__conn.execute(
        "UPDATE memories_fts SET content = ? WHERE rowid = ?",
        (enriched, memory_id)
    )
    self._guarded_commit()
```

Let me find the exact method name and signature:
```
METHOD update_memory_compression(memory_id, content, embedding, compression_level,
                                  original_content=None)
```

### Path 4: upsert_memory() UPDATE path — lines ~2210-2224

Already has explicit FTS sync. No trigger needed. No change required.

```
CURRENT (already correct):
    self.__conn.execute(
        "UPDATE memories SET content=?, embedding=?, heat=?, ... WHERE id=?", ...
    )
    self._guarded_commit()
    # Explicit FTS sync already present:
    enriched = self._enrich_content_for_fts(content)
    self.__conn.execute(
        "UPDATE memories_fts SET content=? WHERE rowid=?",
        (enriched, memory_id)
    )
    self._guarded_commit()
    # Without the trigger, this enriched content will PERSIST correctly
    # because no subsequent non-content UPDATE will revert it.
```

### Path 5: upsert_memory() INSERT path — lines ~2237-2251

Currently: trigger auto-inserts raw content, then manual UPDATE enriches.
After trigger removal: must INSERT explicitly.

```
BEFORE (with trigger):
    cur = self.__conn.execute("INSERT INTO memories(...) VALUES (...)", ...)
    self._guarded_commit()
    new_id = cur.lastrowid
    # Trigger already inserted raw content; now overwrite with enriched:
    enriched = self._enrich_content_for_fts(content)
    self.__conn.execute(
        "UPDATE memories_fts SET content=? WHERE rowid=?",
        (enriched, new_id)
    )

AFTER (no trigger):
    cur = self.__conn.execute("INSERT INTO memories(...) VALUES (...)", ...)
    self._guarded_commit()
    new_id = cur.lastrowid
    # Explicit FTS INSERT (not UPDATE — no trigger pre-inserted)
    enriched = self._enrich_content_for_fts(content)
    self.__conn.execute(
        "INSERT INTO memories_fts(rowid, content) VALUES (?, ?)",
        (new_id, enriched)
    )
    self._guarded_commit()
```

### Path 6: update_memory_full() — lines ~2796-2804

Already has explicit FTS sync for content changes. No trigger needed.

```
CURRENT (already correct):
    self.__conn.execute(f"UPDATE memories SET {set_clause} WHERE id = ?", ...)
    if content is not None:
        enriched = self._enrich_content_for_fts(content)
        self.__conn.execute(
            "UPDATE memories_fts SET content = ? WHERE rowid = ?",
            (enriched, memory_id)
        )
    # Without the trigger, the FTS enrichment is preserved because
    # non-content updates no longer fire a trigger that reverts it.
```

### Path 7: bulk_delete_by_filter() — line ~2741

This calls `delete_memory()` for each matching memory, which we already fixed in Path 2.
No additional change needed.

---

## Summary of changes needed for memories_fts

| Path | Method | Change needed |
|---|---|---|
| 1 | insert_memory() | Replace conditional UPDATE with unconditional INSERT |
| 2 | _delete_memory_inner() | Add explicit DELETE FROM memories_fts |
| 3 | update_memory_compression() | Add explicit UPDATE to memories_fts |
| 4 | upsert_memory() UPDATE | No change — already explicit |
| 5 | upsert_memory() INSERT | Change UPDATE to INSERT |
| 6 | update_memory_full() | No change — already explicit |
| 7 | bulk_delete_by_filter() | No change — delegates to delete_memory() |

**Key insight:** Category B paths (16 non-content UPDATEs) need NO changes.
They were being incorrectly synced by the trigger before. Now they simply don't
touch FTS, which is correct.
