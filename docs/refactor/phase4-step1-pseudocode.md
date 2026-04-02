# Phase 4, Step 1: Fix Double Heat Decay (M14)

## The bug

In `_daemon_loop()` (consolidation.py line 192), each loop iteration checks:
1. Light cycle due? → `_light_cycle()` → calls `_apply_decay()`
2. Medium cycle due? → `_medium_cycle()` → does NOT call `_apply_decay()`
3. Idle? → `_consolidation_cycle()` → calls `_apply_decay()`

When the system transitions from active to idle, there's an iteration where:
- The light cycle is due (60s elapsed since last)
- The idle threshold is crossed (e.g. 300s since last activity)

Both fire in the same iteration. `_apply_decay()` runs twice, squaring the
decay factor for that interval.

## Fix: Track last decay timestamp, skip if already decayed this iteration

Two options:

**Option A: Per-iteration flag**
Set a flag at the start of each loop iteration, check before applying decay.
Simple, but slightly fragile if exception handling is wrong.

**Option B: Timestamp-based deduplication**
Track when decay was last applied. Skip if < DAEMON_CHECK_INTERVAL ago.
More robust — works even if called from `force_consolidate()`.

Going with **Option B** — it's self-correcting and handles edge cases.

---

## Implementation

```
IN consolidation.py, AstrocyteEngine.__init__():

BEFORE:
    self._last_light_cycle: datetime | None = None
    self._last_medium_cycle: datetime | None = None
    self._last_full_activity: datetime | None = None

AFTER:
    self._last_light_cycle: datetime | None = None
    self._last_medium_cycle: datetime | None = None
    self._last_full_activity: datetime | None = None
    self._last_decay_time: datetime | None = None  # Prevents double decay
```

```
IN consolidation.py, _apply_decay():

BEFORE (line 425):
    def _apply_decay(self, stats: dict) -> None:
        now = datetime.now(timezone.utc)
        decay = self._settings.DECAY_FACTOR
        cold = self._settings.COLD_THRESHOLD
        
        heat_updates: list[tuple[int, float]] = []
        for mem in self._storage.get_all_memories_for_decay():
            ...

AFTER:
    def _apply_decay(self, stats: dict) -> None:
        now = datetime.now(timezone.utc)
        
        # Skip if decay was already applied recently (prevents double-decay
        # when light cycle and full cycle fire in the same iteration)
        min_gap = self._settings.DAEMON_CHECK_INTERVAL * 0.9  # 90% of check interval
        IF self._last_decay_time is not None:
            since_last = (now - self._last_decay_time).total_seconds()
            IF since_last < min_gap:
                RETURN  # Already decayed this iteration
        
        self._last_decay_time = now
        
        decay = self._settings.DECAY_FACTOR
        cold = self._settings.COLD_THRESHOLD
        
        heat_updates: list[tuple[int, float]] = []
        for mem in self._storage.get_all_memories_for_decay():
            ...  # rest unchanged
```

### Why 90% of check interval?

`DAEMON_CHECK_INTERVAL` is how often the daemon loop wakes up (default: a few
seconds). Using 90% as the minimum gap ensures:
- Two calls in the same iteration (< 1ms apart) → second is skipped
- Two calls in adjacent iterations (~check_interval apart) → both run
- Edge case: if check_interval is very small, 90% prevents any rounding issues

### What about force_consolidate()?

`force_consolidate()` calls `_consolidation_cycle()` which calls `_apply_decay()`.
With the timestamp gate, if someone calls `force_consolidate()` right after a light
cycle ran decay, the force-consolidate's decay is skipped. This is **correct** —
there's no reason to decay twice within the same second.

If someone calls `force_consolidate()` in isolation (no daemon running), the
`_last_decay_time` will be None or stale, so decay runs normally.
