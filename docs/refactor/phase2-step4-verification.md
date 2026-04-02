# Phase 2, Step 4: Verification

## 4a. Static verification

```
# 1. No FTS triggers exist in code
grep -n 'CREATE TRIGGER.*memories_fts' vaire/storage.py
# Expected: ZERO results

# 2. All INSERT INTO memories paths have matching INSERT INTO memories_fts
grep -n 'INSERT INTO memories(' vaire/storage.py
# Then verify each has a corresponding INSERT INTO memories_fts nearby

# 3. DELETE FROM memories has matching DELETE FROM memories_fts
grep -n 'DELETE FROM memories WHERE' vaire/storage.py
# Then verify _delete_memory_inner has DELETE FROM memories_fts

# 4. UPDATE memories SET content has matching UPDATE memories_fts
grep -n "UPDATE memories SET content" vaire/storage.py
# Then verify each has explicit FTS sync

# 5. UPDATE memories SET (non-content) does NOT touch memories_fts
# These paths should have NO FTS operations nearby:
#   SET heat, SET is_stale, SET embedding, SET tags, SET access_count, etc.
```

## 4b. FTS integrity check (manual test)

```
# After running with the new code, verify FTS is consistent:
# In a Python shell or test:

storage = StorageEngine("path/to/test.db")

# Insert a memory with CamelCase content
mid = storage.insert_memory({
    "content": "The StorageEngine handles CamelCase splitting",
    "directory_context": "/test"
})

# Verify FTS has enriched content (CamelCase → "Storage Engine Camel Case")
row = storage.__conn.execute(
    "SELECT content FROM memories_fts WHERE rowid = ?", (mid,)
).fetchone()
assert "Storage" in row[0] and "Engine" in row[0]

# Update a non-content field (heat)
storage.update_memory_heat(mid, 0.5)

# Verify FTS content was NOT reverted
row = storage.__conn.execute(
    "SELECT content FROM memories_fts WHERE rowid = ?", (mid,)
).fetchone()
assert "Storage" in row[0] and "Engine" in row[0]  # Still enriched!
# (Before this fix, the trigger would have reverted it to raw content)

# Delete the memory
storage.delete_memory(mid)

# Verify FTS entry was cleaned up
row = storage.__conn.execute(
    "SELECT content FROM memories_fts WHERE rowid = ?", (mid,)
).fetchone()
assert row is None
```

## 4c. Profiles FTS fix verification

```
# Insert a profile, then update its value
pid = storage.insert_profile("Alice", "preference", "color", "blue")

# Search should find it
results = storage.search_profiles_fts("blue")
assert len(results) > 0

# Update the value (upsert path)
storage.insert_profile("Alice", "preference", "color", "red")

# Search for OLD value should NOT find it
results = storage.search_profiles_fts("blue")
blue_results = [r for r in results if r["attribute_value"] == "blue"]
assert len(blue_results) == 0  # Stale entry removed

# Search for NEW value should find it
results = storage.search_profiles_fts("red")
assert len(results) > 0
```

## 4d. Test suite

```
# Run compression tests (they exercise insert + update paths)
.venv/bin/python -m pytest vaire/tests/test_compression.py -x -q

# Run storage tests
.venv/bin/python -m pytest vaire/tests/test_storage.py -x -q

# Run server tests (exercise remember → update heat cycle)
.venv/bin/python -m pytest vaire/tests/test_server.py -x -q

# Full suite
.venv/bin/python -m pytest vaire/tests/ -x -q \
    --ignore=vaire/tests/test_stress.py \
    --ignore=vaire/tests/test_live_system.py
```

## 4e. Issues resolved

| Issue | Status |
|---|---|
| C3 (FTS enrichment reverted by triggers) | RESOLVED — triggers removed, explicit sync only on content changes |
| M2 (profiles_fts delete uses wrong value) | RESOLVED — uses OLD attribute_value |
