# Phase 2: FTS Trigger Removal + Explicit FTS Sync

## Context

### Problem C3: FTS enrichment silently reverted by triggers

The database has three triggers on the `memories` table:
- `memories_fts_insert`: AFTER INSERT → inserts raw content into memories_fts
- `memories_fts_update`: AFTER UPDATE → overwrites memories_fts with raw content
- `memories_fts_delete`: AFTER DELETE → deletes from memories_fts

The code also manually enriches FTS content (splitting CamelCase, underscored identifiers)
via `_enrich_content_for_fts()` after insert.

**The bug:** There are 19 different `UPDATE memories SET ...` paths in storage.py. Every
single one fires `memories_fts_update`, which replaces the enriched FTS content with the
raw `new.content` value. So the enrichment done at insert time is silently reverted the
first time any field on that memory is updated (heat, tags, embedding, compression, etc.).

### Problem M2: profiles_fts delete uses wrong value

In `insert_profile()` (upsert path), the FTS5 delete command uses the NEW `attribute_value`
parameter instead of the OLD value from the existing row. FTS5 external content tables
require the exact original values for delete operations. This leaves stale FTS entries.

## Approach

1. Remove all three triggers on `memories` table
2. Add explicit FTS sync to every write path that touches memories
3. Fix the profiles_fts delete to use OLD value
4. This is a schema migration (append-only) + code changes in storage.py only

## Files Modified

| File | Change |
|---|---|
| vaire/storage.py | Remove triggers, add explicit FTS in write methods, fix profiles_fts |

No other files change — all FTS sync is internal to StorageEngine.

## Dependency

Phase 2 should run AFTER Phase 1 (which renames `_conn` to `__conn`). The pseudocode
below uses `self.__conn` to match the post-Phase-1 state. If Phase 2 runs before Phase 1,
use `self._conn` instead.
