# Step 3: _conn Lockdown + Anchor LIKE Fix

After all Step 2 migrations are complete, lock down direct access.

---

## 3a. Rename _conn to __conn (name mangling)

```
IN storage.py:

BEFORE (the property, ~line 75):
    @property
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._create_connection()
            self._local.conn = conn
        return conn

AFTER:
    @property
    def __conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._create_connection()
            self._local.conn = conn
        return conn

    # Python name mangling: __conn becomes _StorageEngine__conn internally.
    # Within storage.py, self.__conn works normally.
    # From outside, self._storage._conn raises AttributeError.
    # From outside, self._storage._StorageEngine__conn still works (for tests).

THEN: Find-and-replace within storage.py:
    self._conn  →  self.__conn
    (approximately 200+ occurrences within storage.py itself)
```

---

## 3b. Update all internal _conn references in storage.py

This is a mechanical find-and-replace **within storage.py only**.
Every `self._conn` becomes `self.__conn`.

Key locations:
- All insert_* methods
- All update_* methods  
- All get_* methods (existing ones)
- All delete_* methods
- execute_write, execute_writes
- begin_transaction, commit, rollback
- _guarded_commit
- close()
- Schema migration methods
- The new domain methods from Step 1 (which already use self.__conn)

Verification:
```
grep 'self\._conn' vaire/storage.py
# Should return ZERO results (all should be self.__conn now)

grep 'self\.__conn' vaire/storage.py
# Should return 200+ results (all internal usage)
```

---

## 3c. Verify no external _conn access remains

```
grep -r '_storage._conn\|storage\._conn' vaire/ --include='*.py' \
    | grep -v test \
    | grep -v __pycache__ \
    | grep -v hooks/ \
    | grep -v __main__.py

# Expected: ZERO results
# hooks/ and __main__.py are documented exceptions (separate processes)
```

---

## 3d. Test file compatibility

Test files use `storage._conn` extensively for setup/teardown.
After name mangling, they need to use the mangled name:

```
# In test files, the mangled name still works:
storage._StorageEngine__conn.execute(...)

# But this is ugly. Alternative: add a test-only accessor:
# In storage.py, at the end of the class:

IF os.environ.get("VAIRE_TEST_MODE") or os.environ.get("PYTEST_CURRENT_TEST"):
    @property
    def _test_conn(self):
        """Direct connection access for test setup/teardown only."""
        return self.__conn

# Then tests use:
    storage._test_conn.execute(...)
    storage._test_conn.commit()

# This is cleaner than the mangled name and makes intent clear.
```

---

## 3e. Anchor LIKE wildcard fix (L26) — already handled

The `get_anchored_memories()` and `get_recent_memories()` methods from Step 1
already use the corrected pattern:

```
BEFORE (restoration.py, hooks, __main__.py):
    tags LIKE '%_anchor%'
    # _ is a SQL single-char wildcard — matches 'xanchor', '1anchor', etc.

AFTER (in the new domain methods):
    tags LIKE '%"_anchor"%'
    # The quotes ensure we match the JSON-serialized tag literal
```

For hooks/__main__.py (which still use direct sqlite3):
- These also use LIKE '%_anchor%' and should be updated to '%"_anchor"%'
- This is a one-line fix in each file, done during Step 2m (restoration.py)
  or as a separate small patch.

---

## Execution order for Step 3

1. Add `_test_conn` property (gated by PYTEST_CURRENT_TEST)
2. Rename `_conn` → `__conn` in storage.py (mechanical find-replace)
3. Run: `grep 'self\._conn' vaire/storage.py` — verify zero hits
4. Run: `grep '_storage._conn' vaire/ -r --include='*.py' | grep -v test | grep -v __pycache__ | grep -v hooks | grep -v __main__` — verify zero hits
5. Run full test suite
6. Fix any test files that break (use `_test_conn` or `_StorageEngine__conn`)
