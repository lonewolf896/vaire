# Phase 2, Step 1: Remove FTS Triggers

## 1a. Schema migration: drop all three triggers

Add a new migration to `_migrate_schema()` in storage.py. Per convention, migrations
are append-only — we never modify an existing one.

```
IN storage.py, _migrate_schema() — append at end of existing migrations:

METHOD _migrate_schema(self):
    ... (existing migrations) ...

    # --- Migration: Remove FTS triggers (Phase 2 refactor) ---
    # These triggers silently revert enriched FTS content on any UPDATE.
    # FTS sync is now handled explicitly in insert/update/delete methods.
    self.__conn.execute("DROP TRIGGER IF EXISTS memories_fts_insert")
    self.__conn.execute("DROP TRIGGER IF EXISTS memories_fts_update")
    self.__conn.execute("DROP TRIGGER IF EXISTS memories_fts_delete")
    self.__conn.commit()
```

## 1b. Remove trigger creation from _init_schema()

The triggers are currently created in `_init_schema()` at lines 277-304.
After migration drops them, we must NOT recreate them on fresh DBs either.

```
IN storage.py, _init_schema() — REMOVE lines 277-304:

BEFORE (lines 277-304):
    # Triggers for FTS sync — drop first to allow updates
    c.execute("DROP TRIGGER IF EXISTS memories_fts_insert")
    c.execute("DROP TRIGGER IF EXISTS memories_fts_update")
    for trigger_sql in [
        """CREATE TRIGGER IF NOT EXISTS memories_fts_insert ...""",
        """CREATE TRIGGER IF NOT EXISTS memories_fts_update ...""",
        """CREATE TRIGGER IF NOT EXISTS memories_fts_delete ...""",
    ]:
        c.execute(trigger_sql)

AFTER:
    # FTS triggers removed — FTS sync is explicit in insert/update/delete methods.
    # (Migration in _migrate_schema drops triggers for existing DBs.)
```
