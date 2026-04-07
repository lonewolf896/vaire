# TASK-044 Phase 3c: TaskSyncThread — GitLab Sync Background Thread

## Overview

Background daemon thread that syncs local task cache to GitLab every 30s.
Local cache is the working copy (instant mutations under lock). GitLab is
the durable backup. Sync runs OUTSIDE the lock to avoid blocking agents.


## 3c-1. TaskSyncThread.__init__

```
class TaskSyncThread:
    """Background thread that syncs local task cache to GitLab every 30s.

    Protocol:
    1. Acquire _task_lock, check _dirty flag, snapshot cache, release lock
    2. If not dirty, sleep and continue
    3. Outside lock: read GitLab current state
    4. Merge: local wins for active tasks, history is union (dedupe by ts+action)
    5. Write merged state to GitLab with optimistic locking (last_commit_id)
    6. If 409 conflict: re-read, re-merge, retry (max 3)
    7. If GitLab unreachable: _dirty stays True, retry next cycle
    8. On success: acquire lock, update cache with merged state, release lock

    Security:
    - GitLab token NEVER appears in commit messages, logs, or errors
    - Commit messages: "vaire-task-sync: {summary}" — task IDs only, no content
    - Version monotonicity checked — tamper detection
    - schema_version mismatch blocks merge entirely
    """

    MAX_RETRIES = 3

    def __init__(self, task_engine: TaskEngine, gitlab: GitLabClient, settings):
        # Shared state — borrowed from TaskEngine, not copied
        self._engine = task_engine
        self._lock = task_engine._task_lock       # threading.Lock
        self._cache = task_engine._task_cache      # dict — the live cache
        self._dirty = task_engine._dirty           # list[bool] length 1, mutable ref
        # NOTE: _dirty is a mutable container (e.g. [False]) so both TaskEngine
        # and TaskSyncThread can read/write the same flag. Alternatively, use
        # threading.Event. The key constraint: reads/writes to _dirty MUST be
        # under _lock.

        self._gitlab = gitlab
        self._interval = settings.TASK_SYNC_INTERVAL       # default 30s
        self._file_path = settings.GITLAB_TASKS_FILE        # "tasks.json"
        self._branch = settings.GITLAB_TASKS_BRANCH         # "main"

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_commit_id: str | None = None             # optimistic lock token

        self._logger = logging.getLogger("vaire.task_sync")
```


## 3c-2. start / stop

```
    def start(self) -> None:
        """Spawn daemon thread. Idempotent — no-op if already running."""
        if self._thread is not None and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._sync_loop,
            name="vaire-task-sync",
            daemon=True,          # dies with main process
        )
        self._thread.start()
        self._logger.info("Task sync thread started (interval=%ds)", self._interval)

    def stop(self) -> None:
        """Signal stop, attempt final sync, join with timeout.

        Called during server shutdown. Best-effort final push.
        """
        self._stop_event.set()

        # Final sync attempt — push whatever is dirty
        try:
            self._sync_once()
        except Exception:
            self._logger.warning("Final sync failed during shutdown", exc_info=True)

        if self._thread is not None:
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                self._logger.warning("Task sync thread did not exit within 5s timeout")
            self._thread = None
```


## 3c-3. _sync_loop

```
    def _sync_loop(self) -> None:
        """Main loop: sleep, check dirty, sync. Runs until _stop_event is set."""

        # On first iteration, always try to sync (startup merge)
        first_run = True

        while not self._stop_event.is_set():
            try:
                if first_run:
                    self._startup_merge()
                    first_run = False
                else:
                    self._sync_once()
            except Exception:
                # Catch-all: never let the sync thread die from an unhandled exception
                # _dirty stays True so we retry next cycle
                self._logger.error("Sync cycle failed", exc_info=True)

            # Sleep in small increments so stop_event is responsive
            # Wait returns True if the event is set (stop requested)
            if self._stop_event.wait(timeout=self._interval):
                break  # stop requested
```


## 3c-4. _startup_merge

```
    def _startup_merge(self) -> None:
        """On startup: if local tasks.json exists and GitLab is reachable,
        merge local state with remote and push.

        This handles the case where the server crashed with un-synced changes.
        Local /data/tasks.json IS the pending state — no separate log needed.
        """

        # Step 1: snapshot local cache under lock
        with self._lock:
            local_snapshot = deep_copy(self._cache)
            has_local = bool(local_snapshot.get("tasks"))

        if not has_local:
            # No local state — pull from GitLab to seed cache
            try:
                content, commit_id = self._gitlab.read_file(self._file_path, self._branch)
                remote = json.loads(content)
                self._validate_schema_version(remote)
                with self._lock:
                    self._cache.update(remote)
                    self._dirty[0] = False
                self._last_commit_id = commit_id
                self._logger.info("Startup: seeded local cache from GitLab")
            except FileNotFoundError:
                self._logger.info("Startup: no remote tasks.json — starting fresh")
            except Exception:
                self._logger.warning("Startup: GitLab unreachable — using local state", exc_info=True)
            return

        # Step 2: local exists — merge with remote
        try:
            content, commit_id = self._gitlab.read_file(self._file_path, self._branch)
            remote = json.loads(content)
            self._validate_schema_version(remote)
            self._last_commit_id = commit_id

            merged = self._merge(local_snapshot, remote)
            self._push_merged(merged, "startup-merge")
        except FileNotFoundError:
            # Remote doesn't exist yet — push local as initial state
            self._push_merged(local_snapshot, "initial-push")
        except Exception:
            self._logger.warning("Startup merge failed — local state preserved, will retry", exc_info=True)
            with self._lock:
                self._dirty[0] = True
```


## 3c-5. _sync_once

```
    def _sync_once(self) -> None:
        """Single sync cycle.

        1. Under lock: check dirty, snapshot cache
        2. If not dirty: return (nothing to push)
        3. Outside lock: read remote, merge, push
        4. On success: update cache with merged state, clear dirty
        5. On conflict (409): retry up to MAX_RETRIES
        6. On unreachable: set dirty, log, return (retry next cycle)
        """

        # Step 1: snapshot under lock
        with self._lock:
            if not self._dirty[0]:
                return
            local_snapshot = deep_copy(self._cache)

        # Step 2: read remote (OUTSIDE lock — may be slow)
        try:
            content, commit_id = self._gitlab.read_file(self._file_path, self._branch)
            remote = json.loads(content)
            self._validate_schema_version(remote)
            self._last_commit_id = commit_id
        except FileNotFoundError:
            # Remote file deleted or never created — push local as-is
            remote = None
        except (GitLabError, ConnectionError, httpx.TimeoutException) as e:
            # SECURITY: never log e.args (might contain token echo from server)
            self._logger.warning("GitLab unreachable during sync — will retry next cycle")
            return  # _dirty stays True
        except json.JSONDecodeError:
            self._logger.error("Remote tasks.json is corrupt — pushing local state to overwrite")
            remote = None

        # Step 3: merge
        if remote is not None:
            merged = self._merge(local_snapshot, remote)
        else:
            merged = local_snapshot

        # Step 4: push with retry on 409
        self._push_with_retry(merged)
```


## 3c-6. _push_with_retry

```
    def _push_with_retry(self, merged: dict) -> None:
        """Push merged state to GitLab. Retry on 409 conflict.

        On 409: re-read remote, re-merge with CURRENT local snapshot (not stale),
        retry push. Max MAX_RETRIES attempts.

        On success: acquire lock, update cache with final merged state, clear dirty.
        """

        for attempt in range(self.MAX_RETRIES):
            try:
                # Build commit message — task IDs only, NEVER content or token
                modified_ids = self._diff_task_ids(merged)
                summary = f"{len(modified_ids)} tasks" if len(modified_ids) > 5 \
                    else ",".join(modified_ids) if modified_ids else "no changes"
                commit_msg = f"vaire-task-sync: {summary}"

                content_str = json.dumps(merged, indent=2, sort_keys=True)
                new_commit_id = self._gitlab.write_file(
                    self._file_path,
                    content_str,
                    commit_msg,
                    self._branch,
                    self._last_commit_id,   # optimistic lock
                )
                self._last_commit_id = new_commit_id

                # SUCCESS — update local cache under lock
                with self._lock:
                    self._cache.update(merged)
                    self._dirty[0] = False

                self._logger.info("Sync pushed: %s (commit=%s)", summary, new_commit_id[:8])
                return

            except GitLabConflictError:
                # 409 — remote was modified between our read and write
                self._logger.info("Conflict on attempt %d/%d — re-reading and re-merging",
                                  attempt + 1, self.MAX_RETRIES)

                if attempt + 1 >= self.MAX_RETRIES:
                    self._logger.error("Max retries exceeded on 409 conflict — will retry next cycle")
                    return  # _dirty stays True

                # Re-read remote
                try:
                    content, commit_id = self._gitlab.read_file(self._file_path, self._branch)
                    remote = json.loads(content)
                    self._validate_schema_version(remote)
                    self._last_commit_id = commit_id
                except Exception:
                    self._logger.warning("Failed to re-read after conflict — will retry next cycle")
                    return

                # Re-snapshot local (it may have changed while we were pushing)
                with self._lock:
                    local_snapshot = deep_copy(self._cache)

                # Re-merge with fresh data
                merged = self._merge(local_snapshot, remote)
                # Loop continues to next attempt

            except (GitLabError, ConnectionError, httpx.TimeoutException):
                self._logger.warning("GitLab write failed — will retry next cycle")
                return  # _dirty stays True
```


## 3c-7. _merge

```
    def _merge(self, local: dict, remote: dict) -> dict:
        """Merge local and remote task state.

        Merge strategy per task (keyed by task ID string):
          - Exists only in local  -> include (new local task)
          - Exists only in remote -> include (created via GitLab UI or another instance)
          - Exists in both        -> field-level merge (see below)

        Field-level merge for tasks existing in both:
          agent_block:
            - heartbeat: newer timestamp wins
            - agent_id: follows heartbeat winner
            - (if heartbeat stale per TTL, block is cleared regardless)
          history:
            - union of both lists
            - dedupe by (ts, action, by) tuple
            - sort by ts ascending
          status:
            - if EITHER side is "done", result is "done" (done is terminal)
            - else: more-progressed status wins
              progression: open < assigned < in_progress < blocked < review < done
          acceptance_criteria:
            - local wins (agent may have just checked a box)
          title, description, priority, role:
            - remote wins (these are edited by Vale/creator, not by agents)
          tags:
            - union of both tag sets

        Top-level fields:
          version  = max(local.version, remote.version) + 1
          next_id  = max(local.next_id, remote.next_id)
          schema_version = must match (validated before merge is called)

        Security:
          - Version monotonicity: if remote.version < local.version AND remote
            is not empty, log SECURITY WARNING (possible tampering/rollback).
            Still merge, but alert.
          - Token never in any field of merged output.

        Returns: new merged dict (deep copy — does not mutate inputs)
        """

        STATUS_ORDER = {
            "open": 0, "assigned": 1, "in_progress": 2,
            "blocked": 3, "review": 4, "done": 5,
        }

        merged = {
            "schema_version": local.get("schema_version", 1),
            "version": max(local.get("version", 0), remote.get("version", 0)) + 1,
            "next_id": max(local.get("next_id", 1), remote.get("next_id", 1)),
            "tasks": {},
        }

        # SECURITY: version monotonicity check
        if (remote.get("version", 0) < local.get("version", 0)
                and remote.get("tasks")):
            self._logger.warning(
                "SECURITY: remote version (%d) < local version (%d) — "
                "possible tampering or rollback",
                remote.get("version", 0), local.get("version", 0),
            )

        all_task_ids = set(local.get("tasks", {}).keys()) | set(remote.get("tasks", {}).keys())

        for tid in all_task_ids:
            l_task = local.get("tasks", {}).get(tid)
            r_task = remote.get("tasks", {}).get(tid)

            if l_task is None:
                # Remote-only task — include as-is
                merged["tasks"][tid] = deep_copy(r_task)
                continue

            if r_task is None:
                # Local-only task — include as-is
                merged["tasks"][tid] = deep_copy(l_task)
                continue

            # Both exist — field-level merge
            m = deep_copy(l_task)  # start from local

            # title, description, priority, role: remote wins
            for field in ("title", "description", "priority", "role"):
                if field in r_task:
                    m[field] = r_task[field]

            # status: done is terminal, else more-progressed wins
            l_status = l_task.get("status", "open")
            r_status = r_task.get("status", "open")
            if l_status == "done" or r_status == "done":
                m["status"] = "done"
            else:
                l_ord = STATUS_ORDER.get(l_status, 0)
                r_ord = STATUS_ORDER.get(r_status, 0)
                m["status"] = l_status if l_ord >= r_ord else r_status

            # acceptance_criteria: local wins (already in m from deep_copy)
            # No action needed.

            # agent_block: newer heartbeat wins
            l_block = l_task.get("agent_block", {})
            r_block = r_task.get("agent_block", {})
            l_hb = l_block.get("heartbeat", "")
            r_hb = r_block.get("heartbeat", "")
            if r_hb > l_hb:
                m["agent_block"] = deep_copy(r_block)
            # else: local block stays (already in m)

            # history: union, dedupe by (ts, action, by)
            l_history = l_task.get("history", [])
            r_history = r_task.get("history", [])
            seen = set()
            merged_history = []
            for entry in l_history + r_history:
                key = (entry.get("ts", ""), entry.get("action", ""), entry.get("by", ""))
                if key not in seen:
                    seen.add(key)
                    merged_history.append(entry)
            merged_history.sort(key=lambda e: e.get("ts", ""))
            m["history"] = merged_history

            # tags: union
            l_tags = set(l_task.get("tags", []))
            r_tags = set(r_task.get("tags", []))
            m["tags"] = sorted(l_tags | r_tags)

            merged["tasks"][tid] = m

        return merged
```


## 3c-8. _validate_schema_version

```
    def _validate_schema_version(self, remote: dict) -> None:
        """Refuse to merge if schema_version differs.

        Security: prevents silent data corruption if task schema changes
        and two instances with different versions try to sync.

        Raises ValueError if mismatch.
        """
        local_sv = self._cache.get("schema_version", 1)
        remote_sv = remote.get("schema_version", 1)

        if local_sv != remote_sv:
            self._logger.error(
                "Schema version mismatch: local=%d remote=%d — refusing to merge. "
                "Deploy matching versions before syncing.",
                local_sv, remote_sv,
            )
            raise ValueError(
                f"Task schema_version mismatch: local={local_sv} remote={remote_sv}"
            )
```


## 3c-9. _diff_task_ids (commit message helper)

```
    def _diff_task_ids(self, merged: dict) -> list[str]:
        """Return list of task IDs that differ between merged and last-known remote.

        Used to build commit messages. Returns task IDs only — NEVER task content,
        titles, or any user-generated text in commit messages.

        SECURITY: commit messages must contain only synthetic identifiers.
        """
        # Compare merged tasks against current cache (which reflects last-known state)
        with self._lock:
            current_ids = set(self._cache.get("tasks", {}).keys())

        merged_ids = set(merged.get("tasks", {}).keys())

        # New tasks + tasks whose content changed (simplified: just report all)
        changed = merged_ids ^ current_ids  # symmetric difference = new/removed
        # For tasks in both, we'd need deep comparison — not worth it for a commit msg
        # Just report count if too many
        return sorted(changed) if len(changed) <= 10 else sorted(merged_ids)
```


## 3c-10. init_engines integration

```
# In init_engines() — after TaskEngine is created:

_task_sync: TaskSyncThread | None = None

if settings.gitlab_enabled:
    from vaire.gitlab_client import GitLabClient
    _gitlab = GitLabClient(
        settings.GITLAB_API_URL,
        settings.GITLAB_PROJECT_ID,
        settings.GITLAB_TOKEN,
    )
    _task_sync = TaskSyncThread(_task_engine, _gitlab, settings)
    _task_sync.start()
    logger.info("GitLab task sync enabled (interval=%ds)", settings.TASK_SYNC_INTERVAL)
else:
    _gitlab = None
    logger.info("GitLab task sync disabled — running local-only")
```


## 3c-11. Shutdown integration

```
# In shutdown handler (atexit or signal handler):

if _task_sync is not None:
    _task_sync.stop()       # final sync attempt + join(5s)
if _gitlab is not None:
    _gitlab.close()         # close httpx client
```


## 3c-12. TaskEngine._dirty contract

```
# TaskEngine must expose _dirty as a mutable container for TaskSyncThread:

class TaskEngine:
    def __init__(self, settings):
        self._task_lock = threading.Lock()
        self._task_cache: dict = {}              # the live cache
        self._dirty: list[bool] = [False]        # mutable flag container

    def _mark_dirty(self) -> None:
        """Called after every local mutation (create, update, heartbeat, etc.).
        Must be called INSIDE _task_lock."""
        self._dirty[0] = True

    # Every mutating method pattern:
    def create_task(self, ...):
        with self._task_lock:
            # ... mutate self._task_cache ...
            self._mark_dirty()
            # ... write local /data/tasks.json ...
        return result
```


## Security Annotations Summary

| Concern | Mitigation |
|---|---|
| Token leakage in commit messages | Commit msg contains only "vaire-task-sync:" + task IDs, never content |
| Token leakage in logs | GitLab errors logged without response body; repr masks token |
| Version rollback / tampering | Version monotonicity check logs SECURITY warning if remote < local |
| Schema version mismatch | Hard refuse to merge; raises ValueError |
| Stale lock blocking agents | GitLab I/O runs OUTSIDE _task_lock; lock held only for dict snapshot/update |
| Thread crash | Catch-all in _sync_loop; thread never dies from unhandled exception |
| Data loss on crash | Local /data/tasks.json is the pending state; startup merge recovers |
| Race between sync and mutation | Re-snapshot local under lock after 409 conflict before re-merge |
| Infinite retry storm | MAX_RETRIES=3 per cycle; on exhaustion, defers to next 30s cycle |
| Daemon thread cleanup | stop() does final sync + join(5s timeout); thread is daemon (dies with process) |


## Offline Resilience Summary

1. GitLab unreachable during sync: `_dirty` stays True, retry next 30s cycle
2. Server crashes with un-synced changes: local `/data/tasks.json` persists (volume mount), startup merge pushes on next boot
3. Remote file deleted: push local state as initial creation
4. Remote file corrupt (invalid JSON): overwrite with local state, log error
5. No separate pending log needed: the local file IS the pending state
