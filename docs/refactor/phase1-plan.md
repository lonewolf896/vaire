# Phase 1: StorageEngine Domain Method Lockdown

## Context

The code review found 71 issues across the Vaire codebase. The #1 systemic problem is that 17 production files bypass StorageEngine's public API by accessing `self._storage._conn` directly (~60 call sites). This defeats the write lock, thread-local connection management, and makes the codebase fragile to any storage internals change.

This plan adds domain-specific query methods to StorageEngine, migrates all 60 external `_conn` call sites to use them, then makes `_conn` inaccessible from outside. This collapses issues C1, C2, M9, M15, M16, and all 30+ architecture violations identified in the review.

## Steps

1. Add ~38 new domain methods to storage.py (Step 1, see phase1-step1-pseudocode.md)
2. Migrate 60 call sites across 17 files (Step 2, see phase1-step2-pseudocode.md)
3. Rename `_conn` to `__conn` for name mangling lockdown (Step 3, see phase1-step3-pseudocode.md)
4. Run full test suite + QA container validation

## Constraints

- All new methods use strict `heat > ?` (not `>=`) to match existing codebase semantics
- `_row_to_dict`/`_rows_to_dicts` used for consistency on all SELECT * queries
- Hooks/__main__.py are documented exceptions (separate processes, own connections)
- Test files deferred — they use `_StorageEngine__conn` mangled name
- write_queue gets sanctioned `get_write_connection()` escape hatch

## Files Modified

| File | Change |
|---|---|
| vaire/storage.py | Add ~38 new domain methods |
| vaire/knowledge_graph.py | Replace 10 `_conn` calls |
| vaire/cls_store.py | Replace 7 `_conn` calls |
| vaire/curation.py | Replace 9 `_conn` calls |
| vaire/retrieval.py | Replace 7 `_conn` calls |
| vaire/sleep_compute.py | Replace 5 `_conn` calls |
| vaire/server.py | Replace 5 `_conn` calls |
| vaire/metacognition.py | Replace 5 `_conn` calls |
| vaire/consolidation.py | Replace 2 `_conn` calls |
| vaire/rules_engine.py | Replace 4 `_conn` calls |
| vaire/predictive_coding.py | Replace 2 `_conn` calls |
| vaire/causal_discovery.py | Replace 3 `_conn` calls |
| vaire/narrative.py | Replace 1 `_conn` call |
| vaire/restoration.py | Replace 2 `_conn` calls |
| vaire/seed.py | Replace 1 `_conn` call |
| vaire/astrocyte_pool.py | Replace 1 `_conn` call |
| vaire/fractal.py | Replace 3 `_conn` calls |
| vaire/write_queue.py | Replace 3 `_conn` calls |

## Verification

1. `grep -r '_storage._conn\|storage\._conn' vaire/ --include='*.py' | grep -v test | grep -v __pycache__` — zero results (except hooks/__main__)
2. `python -m pytest vaire/tests/ -x -q --ignore=vaire/tests/test_stress.py --ignore=vaire/tests/test_live_system.py` — all pass
3. QA container live tests pass
