# Phase 2, Step 3: Fix profiles_fts Delete Bug (M2)

## The bug

In `insert_profile()` (the upsert/update path), the FTS5 content-sync delete
command uses the NEW `attribute_value` parameter instead of the OLD value from
the existing row.

FTS5 external content tables require that the delete command provides the
**exact original indexed values** — otherwise the delete silently fails and
stale entries accumulate in the FTS index.

## Current code (storage.py line ~1989-2005)

```
BEFORE:
    existing = self.__conn.execute(
        "SELECT * FROM user_profiles WHERE entity_name = ? AND ...", ...
    ).fetchone()
    
    if existing:
        row = dict(existing)
        # ... compute new confidence, evidence ...
        
        # UPDATE the row with new values
        self.__conn.execute(
            "UPDATE user_profiles SET attribute_value = ?, confidence = ?, "
            "evidence_memory_ids = ?, updated_at = ? WHERE id = ?",
            (attribute_value, new_confidence, json.dumps(evidence), now, row["id"])
        )
        
        # Sync FTS — DELETE old entry then INSERT new
        self.__conn.execute(
            "INSERT INTO profiles_fts(profiles_fts, rowid, entity_name, "
            "attribute_type, attribute_key, attribute_value) "
            "VALUES('delete', ?, ?, ?, ?, ?)",
            (row["id"], entity_name, attribute_type, attribute_key,
             attribute_value)  # BUG: uses NEW attribute_value, not OLD
        )
        self.__conn.execute(
            "INSERT INTO profiles_fts(rowid, entity_name, attribute_type, "
            "attribute_key, attribute_value) VALUES(?, ?, ?, ?, ?)",
            (row["id"], entity_name, attribute_type, attribute_key,
             attribute_value)  # This is correct — inserts the new value
        )
```

## Fix

The FTS delete must use the OLD `attribute_value` from `row`, not the NEW
`attribute_value` parameter.

```
AFTER:
    existing = self.__conn.execute(
        "SELECT * FROM user_profiles WHERE entity_name = ? AND ...", ...
    ).fetchone()
    
    if existing:
        row = dict(existing)
        old_attribute_value = row["attribute_value"]  # Save OLD value
        # ... compute new confidence, evidence ...
        
        # UPDATE the row with new values
        self.__conn.execute(
            "UPDATE user_profiles SET attribute_value = ?, confidence = ?, "
            "evidence_memory_ids = ?, updated_at = ? WHERE id = ?",
            (attribute_value, new_confidence, json.dumps(evidence), now, row["id"])
        )
        
        # Sync FTS — DELETE old entry using OLD values, then INSERT new
        self.__conn.execute(
            "INSERT INTO profiles_fts(profiles_fts, rowid, entity_name, "
            "attribute_type, attribute_key, attribute_value) "
            "VALUES('delete', ?, ?, ?, ?, ?)",
            (row["id"], entity_name, attribute_type, attribute_key,
             old_attribute_value)  # FIXED: uses OLD value from existing row
        )
        self.__conn.execute(
            "INSERT INTO profiles_fts(rowid, entity_name, attribute_type, "
            "attribute_key, attribute_value) VALUES(?, ?, ?, ?, ?)",
            (row["id"], entity_name, attribute_type, attribute_key,
             attribute_value)  # Correct: inserts the new value
        )
```

## Impact

This is a one-line fix: change the 5th parameter of the FTS delete command
from `attribute_value` (the function parameter) to `old_attribute_value`
(captured from `row["attribute_value"]` before the update).

Note: `entity_name`, `attribute_type`, and `attribute_key` don't change during
an upsert (they're the lookup keys), so using the function parameters for
those three is correct. Only `attribute_value` can change.
