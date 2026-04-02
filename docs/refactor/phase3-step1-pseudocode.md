# Phase 3, Step 1: Persist CRDT Vector Clock

## Overview

The vector clock must survive server restarts. Use the existing `metadata` table
(same pattern as `last_sleep_cycle` in consolidation.py). The clock is stored as
a JSON string keyed by `"crdt_vector_clock"`.

The clock is loaded on init and saved after every increment.

---

## 1a. Add `set_metadata_value()` to StorageEngine

Phase 1 already adds `get_metadata_value(key)`. We also need the write side.
(The consolidation.py save pattern currently uses `execute_write` with raw SQL.)

```
IN storage.py, add method:

METHOD set_metadata_value(self, key: str, value: str) -> None:
    """Insert or update a key-value pair in the metadata table. [COMMITS]"""
    self.execute_write(
        "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
        (key, value),
    )
```

This uses `execute_write` which holds the write lock — thread-safe.

---

## 1b. Load vector clock on init

```
IN crdt_sync.py, CRDTMemorySync.__init__():

BEFORE:
    def __init__(self, storage: StorageEngine, settings: Settings):
        self._storage = storage
        self._settings = settings
        self._agent_id: str = settings.CRDT_AGENT_ID
        self._vector_clock: dict[str, int] = {self._agent_id: 0}
        self._active_agent_id: str = ""

AFTER:
    def __init__(self, storage: StorageEngine, settings: Settings):
        self._storage = storage
        self._settings = settings
        self._agent_id: str = settings.CRDT_AGENT_ID
        self._vector_clock: dict[str, int] = self._load_vector_clock()
        self._active_agent_id: str = ""
```

---

## 1c. Add `_load_vector_clock()` method

```
IN crdt_sync.py, add method:

METHOD _load_vector_clock(self) -> dict[str, int]:
    """Load persisted vector clock from metadata table.
    
    Falls back to {agent_id: 0} if no clock is stored (fresh DB).
    """
    TRY:
        val = self._storage.get_metadata_value("crdt_vector_clock")
        IF val:
            clock = json.loads(val)
            IF isinstance(clock, dict):
                # Ensure our agent has an entry
                IF self._agent_id not in clock:
                    clock[self._agent_id] = 0
                RETURN clock
    EXCEPT (ValueError, TypeError):
        pass  # corrupt or missing — start fresh
    
    RETURN {self._agent_id: 0}
```

---

## 1d. Add `_save_vector_clock()` method

```
IN crdt_sync.py, add method:

METHOD _save_vector_clock(self) -> None:
    """Persist the current vector clock to the metadata table."""
    TRY:
        self._storage.set_metadata_value(
            "crdt_vector_clock",
            json.dumps(self._vector_clock),
        )
    EXCEPT Exception:
        logger.debug("Could not persist CRDT vector clock")
```

---

## 1e. Save clock after every increment

There are two increment paths:

### Path 1: `increment_clock()` — called by `tag_provenance()`

```
BEFORE:
    def increment_clock(self) -> dict[str, int]:
        self._vector_clock[self._agent_id] = (
            self._vector_clock.get(self._agent_id, 0) + 1
        )
        return dict(self._vector_clock)

AFTER:
    def increment_clock(self) -> dict[str, int]:
        self._vector_clock[self._agent_id] = (
            self._vector_clock.get(self._agent_id, 0) + 1
        )
        self._save_vector_clock()
        return dict(self._vector_clock)
```

### Path 2: `_increment_clock_for_agent()` — called by `tag_write()`

```
BEFORE:
    def _increment_clock_for_agent(self, agent_id: str) -> dict[str, int]:
        self._vector_clock[agent_id] = self._vector_clock.get(agent_id, 0) + 1
        return dict(self._vector_clock)

AFTER:
    def _increment_clock_for_agent(self, agent_id: str) -> dict[str, int]:
        self._vector_clock[agent_id] = self._vector_clock.get(agent_id, 0) + 1
        self._save_vector_clock()
        return dict(self._vector_clock)
```

### Path 3: `resolve_conflict()` — line 280, direct increment

```
BEFORE:
    self._vector_clock[self._agent_id] = (
        self._vector_clock.get(self._agent_id, 0) + 1
    )
    new_clock = json.dumps(dict(self._vector_clock))

AFTER:
    # Use increment_clock() instead of inline increment
    # This also persists the clock
    self.increment_clock()
    new_clock = json.dumps(dict(self._vector_clock))
```

---

## 1f. Merge also updates persisted clock

After `sync_memories()` merges clocks from remote memories, the merged clock
should be persisted. The merge happens inside `merge_memory()` at line 184-192.

However, `merge_memory()` only returns the merged dict — it doesn't update
`self._vector_clock`. The caller (`sync_memories`) writes the merged clock
to the specific memory but doesn't update the instance clock.

**Fix:** After processing all remote memories in `sync_memories`, update the
instance clock to reflect any new agents seen in remote clocks, then persist.

```
IN sync_memories(), after the for loop (after line 381):

BEFORE:
    return stats

AFTER:
    # Update instance clock with any new agents seen from remotes
    for remote in remote_memories:
        TRY:
            remote_clock = json.loads(remote.get("vector_clock", "{}"))
        EXCEPT (ValueError, TypeError):
            continue
        for agent, count in remote_clock.items():
            current = self._vector_clock.get(agent, 0)
            IF count > current:
                self._vector_clock[agent] = count
    self._save_vector_clock()
    
    return stats
```

---

## Performance note

`_save_vector_clock()` calls `set_metadata_value()` which does `INSERT OR REPLACE`
with a commit. On a busy system, `increment_clock()` is called once per `remember()`
call, so this adds one small write per memory store. At ~1000 memories total and
typical usage patterns, this is negligible.

If it ever becomes a concern, the save could be debounced (save every N increments
or every T seconds), but that trades durability for performance. For now, save on
every increment — correctness first.
