# TASK-044 Phase 3a: TaskEngine Class (vaire/task_engine.py)

Local-first mutable task cache. GitLab sync thread is Phase 3c (not here).


## 3A-1. Constants and Imports

```
import json
import logging
import os
import socket
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vaire.config import Settings

logger = logging.getLogger(__name__)

# ── Valid enum values ────────────────────────────────────────────────────
VALID_STATUSES = {"open", "in_progress", "done", "on_hold"}
VALID_PRIORITIES = {"critical", "high", "medium", "low"}

# Statuses that mean "available to claim"
CLAIMABLE_STATUSES = {"open", "on_hold"}

# Empty task file skeleton — used when no seed and no existing file
_EMPTY_STORE = {
    "schema_version": 1,
    "version": 0,
    "next_id": 1,
    "tasks": {},
}
```


## 3A-2. TaskEngine.__init__

```
class TaskEngine:
    """Local-first task cache with thread-safe mutable operations.

    All public methods are serialized by _task_lock.
    agent_id is set by the server dispatch layer — callers cannot spoof it.
    """

    def __init__(self, settings: Settings):
        self._settings = settings
        self._task_lock = threading.Lock()
        # Security: lock serializes ALL reads AND writes to _task_cache.
        # This prevents TOCTOU races on claim checks, ownership checks, etc.

        self._data_path: Path = settings.task_data_path_resolved
        # /data/tasks.json — container-writable, host-mounted volume

        self._task_cache: dict = {}
        # In-memory working copy. Only modified under _task_lock.
        # Only flushed to disk via _flush_local().

        self._load_or_seed()

    def _load_or_seed(self):
        """Load task state from disk, or seed from baked-in file, or create empty.

        Priority order:
        1. /data/tasks.json exists and is valid JSON -> load it
        2. /app/reference/tasks-seed.json exists -> copy it as starting state
        3. Neither exists -> create empty skeleton

        Security: seed file is read-only (baked into Docker image).
        We COPY the data, not reference it — mutations go to /data/ only.
        """
        if self._data_path.exists():
            try:
                raw = json.loads(self._data_path.read_text())
                # Basic schema validation
                if raw.get("schema_version") != 1:
                    raise ValueError(f"Unknown schema_version: {raw.get('schema_version')}")
                if "tasks" not in raw or "version" not in raw or "next_id" not in raw:
                    raise ValueError("Missing required top-level keys")
                self._task_cache = raw
                logger.info(
                    "TaskEngine loaded %d tasks from %s (version %d)",
                    len(raw["tasks"]), self._data_path, raw["version"],
                )
                return
            except (json.JSONDecodeError, ValueError, KeyError) as exc:
                # Corrupt file — log and fall through to seed/empty
                logger.error("Failed to load %s: %s — will re-seed", self._data_path, exc)

        # Try seed file
        seed_path = Path("/app/reference/tasks-seed.json")
        if seed_path.exists():
            try:
                raw = json.loads(seed_path.read_text())
                self._task_cache = raw
                self._flush_local()
                logger.info("TaskEngine seeded from %s (%d tasks)", seed_path, len(raw.get("tasks", {})))
                return
            except (json.JSONDecodeError, ValueError) as exc:
                logger.error("Failed to load seed %s: %s", seed_path, exc)

        # Empty skeleton
        self._task_cache = dict(_EMPTY_STORE)  # shallow copy of skeleton
        self._task_cache["tasks"] = {}          # ensure fresh dict
        self._flush_local()
        logger.info("TaskEngine initialized with empty store")
```


## 3A-3. _flush_local (atomic write)

```
    def _flush_local(self):
        """Write _task_cache to /data/tasks.json atomically.

        MUST be called under _task_lock (caller's responsibility).

        Atomic pattern: write to .tmp file, then os.replace() to final path.
        os.replace() is atomic on POSIX (same filesystem) — readers never
        see a half-written file.
        """
        tmp_path = self._data_path.with_suffix(".json.tmp")

        # Ensure parent directory exists (first run on fresh volume)
        self._data_path.parent.mkdir(parents=True, exist_ok=True)

        content = json.dumps(self._task_cache, indent=2, sort_keys=False)
        tmp_path.write_text(content)
        os.replace(str(tmp_path), str(self._data_path))
        # os.replace is atomic on same filesystem — /data/.tmp -> /data/.json
```


## 3A-4. _now helper

```
    @staticmethod
    def _now() -> str:
        """ISO 8601 UTC timestamp. Uses timezone-aware datetime (not deprecated utcnow)."""
        return datetime.now(timezone.utc).isoformat()
```


## 3A-5. _append_history helper

```
    @staticmethod
    def _append_history(task: dict, agent_id: str, host: str, action: str, detail: str = ""):
        """Append an entry to the task's history list.

        History is append-only — no deletions, no edits.
        agent_id and host come from the server layer (not spoofable by caller).
        """
        entry = {
            "ts": TaskEngine._now(),
            "by": agent_id,
            "host": host,
            "action": action,
        }
        if detail:
            entry["detail"] = detail
        task.setdefault("history", []).append(entry)
```


## 3A-6. _increment_version helper

```
    def _increment_version(self):
        """Bump the monotonic version counter.

        Called on every mutation. The GitLab sync thread (Phase 3c) uses this
        for conflict detection: local version must be >= remote version, or
        the push is rejected.
        """
        self._task_cache["version"] = self._task_cache.get("version", 0) + 1
```


## 3A-7. _check_heartbeat_abandoned

```
    @staticmethod
    def _is_abandoned(task: dict, ttl_minutes: int) -> bool:
        """Check if a claimed task's heartbeat has expired.

        An agent is expected to call update_task periodically, which refreshes
        the heartbeat timestamp. If the heartbeat is older than ttl_minutes,
        the task is considered abandoned and can be reclaimed.

        Security: TTL comes from server config, not from the task data itself.
        An agent cannot set its own TTL to avoid abandonment detection.
        """
        agent = task.get("agent")
        if not agent or not agent.get("claimed_by"):
            return False

        heartbeat = agent.get("heartbeat") or agent.get("claimed_at")
        if not heartbeat:
            return True  # no timestamp at all — treat as abandoned

        try:
            hb_dt = datetime.fromisoformat(heartbeat)
            elapsed = datetime.now(timezone.utc) - hb_dt
            return elapsed.total_seconds() > (ttl_minutes * 60)
        except (ValueError, TypeError):
            return True  # unparseable timestamp — treat as abandoned
```


## 3A-8. list_tasks

```
    def list_tasks(
        self,
        status: str | None = None,
        role: str | None = None,
        include_history: bool = False,
    ) -> list[dict]:
        """Filtered read of all tasks.

        Returns a list of task dicts. Each task includes an "_abandoned" flag
        computed from the heartbeat TTL check — this is ephemeral metadata,
        not persisted.

        Thread-safe: acquires _task_lock for the duration of the read.
        Even reads take the lock to get a consistent snapshot (no torn reads
        while a mutation is mid-flight).
        """
        with self._task_lock:
            ttl = self._settings.TASK_HEARTBEAT_TTL
            results = []
            for task_id, task in self._task_cache.get("tasks", {}).items():
                # Filter by status
                if status and task.get("status") != status:
                    continue
                # Filter by role
                if role and task.get("role") != role:
                    continue

                # Build output dict (copy to avoid leaking internal refs)
                out = {
                    "id": task_id,
                    "title": task.get("title", ""),
                    "status": task.get("status", ""),
                    "priority": task.get("priority", "medium"),
                    "role": task.get("role", ""),
                    "directory": task.get("directory", ""),
                    "description": task.get("description", ""),
                    "depends_on": task.get("depends_on", []),
                    "acceptance_criteria": task.get("acceptance_criteria", []),
                    "context_queries": task.get("context_queries", []),
                    "on_completion": task.get("on_completion"),
                    "created_at": task.get("created_at", ""),
                    "created_by": task.get("created_by", ""),
                    "updated_at": task.get("updated_at", ""),
                    "completed_at": task.get("completed_at"),
                    "result": task.get("result"),
                    # Ephemeral computed flag — not stored in tasks.json
                    "_abandoned": self._is_abandoned(task, ttl),
                }

                # Include agent block (claimed_by, progress, etc.)
                agent = task.get("agent")
                if agent:
                    out["agent"] = dict(agent)  # shallow copy

                # History is optional — can be large
                if include_history:
                    out["history"] = list(task.get("history", []))

                results.append(out)

            return results
```


## 3A-9. get_task

```
    def get_task(self, task_id: str) -> dict | None:
        """Single task read by ID. Returns full task dict or None.

        Always includes history (single-task reads are detail views).
        Includes _abandoned flag.
        """
        with self._task_lock:
            task = self._task_cache.get("tasks", {}).get(task_id)
            if task is None:
                return None

            ttl = self._settings.TASK_HEARTBEAT_TTL
            out = dict(task)  # shallow copy
            out["id"] = task_id
            out["_abandoned"] = self._is_abandoned(task, ttl)
            return out
```


## 3A-10. create_task

```
    def create_task(
        self,
        agent_id: str,
        host: str,
        title: str,
        role: str,
        priority: str = "medium",
        directory: str = "",
        description: str = "",
        acceptance_criteria: list[dict] | None = None,
        depends_on: list[str] | None = None,
        context_queries: list[str] | None = None,
        on_completion: str | None = None,
    ) -> dict:
        """Create a new task. Restricted to allowed agent_id prefixes.

        Security:
        - Only agents whose agent_id starts with a prefix in TASK_CREATE_ALLOWED
          can create tasks. This prevents arbitrary agents from flooding the
          task queue. The prefix list is set in config (e.g. "groomer-,creator").
        - agent_id is set by the server dispatch layer — cannot be spoofed.

        Returns the created task dict (including assigned ID).
        """
        # ── Authorization check ──────────────────────────────────────────
        allowed_prefixes = self._settings.task_create_allowed_list
        if not any(agent_id.startswith(prefix) for prefix in allowed_prefixes):
            raise PermissionError(
                f"Agent {agent_id!r} not authorized to create tasks. "
                f"Allowed prefixes: {allowed_prefixes}"
            )

        # ── Input validation ─────────────────────────────────────────────
        if not title or not title.strip():
            raise ValueError("Task title is required")
        if priority not in VALID_PRIORITIES:
            raise ValueError(f"Invalid priority {priority!r}. Must be one of: {VALID_PRIORITIES}")
        # role is freeform (matches Vaire role names, not a fixed enum)

        # ── Validate acceptance_criteria structure ────────────────────────
        criteria = []
        if acceptance_criteria:
            for i, ac in enumerate(acceptance_criteria):
                if not isinstance(ac, dict) or "text" not in ac:
                    raise ValueError(f"acceptance_criteria[{i}] must have 'text' key")
                criteria.append({
                    "id": ac.get("id", i + 1),
                    "text": str(ac["text"]),
                    "done": bool(ac.get("done", False)),
                })

        now = self._now()

        with self._task_lock:
            # Assign next ID
            next_num = self._task_cache.get("next_id", 1)
            task_id = f"TASK-{next_num:03d}"
            self._task_cache["next_id"] = next_num + 1

            task = {
                "title": title.strip(),
                "status": "open",
                "priority": priority,
                "role": role,
                "directory": directory,
                "description": description,
                "acceptance_criteria": criteria,
                "depends_on": depends_on or [],
                "context_queries": context_queries or [],
                "on_completion": on_completion,
                "created_at": now,
                "created_by": agent_id,
                "updated_at": now,
                "agent": None,
                "history": [],
                "completed_at": None,
                "result": None,
            }

            self._append_history(task, agent_id, host, "created", f"Priority: {priority}, Role: {role}")

            self._task_cache["tasks"][task_id] = task
            self._increment_version()
            self._flush_local()

            # Return copy with ID included
            result = dict(task)
            result["id"] = task_id
            return result
```


## 3A-11. claim_task

```
    def claim_task(
        self,
        task_id: str,
        agent_id: str,
        host: str,
        model: str = "",
        pid: int = 0,
    ) -> dict:
        """Claim an open or abandoned task.

        Security invariants:
        1. ONE-TASK-AT-A-TIME: An agent cannot claim a second task while it
           already holds one in_progress. This prevents a single agent from
           hoarding the queue. Scan the full task list for existing claims.
        2. CLAIMABLE CHECK: Task must be in open/on_hold status, OR it must be
           in_progress but the current holder's heartbeat has expired (abandoned).
        3. ABANDONED RECLAIM: If a task is in_progress but abandoned, the new
           agent can reclaim it. The old claim is wiped and history records the
           reclaim with the old agent's ID for audit.
        4. agent_id comes from server dispatch — cannot be spoofed.
        """
        with self._task_lock:
            task = self._task_cache.get("tasks", {}).get(task_id)
            if task is None:
                raise KeyError(f"Task {task_id!r} not found")

            ttl = self._settings.TASK_HEARTBEAT_TTL

            # ── One-at-a-time check ──────────────────────────────────────
            # Scan ALL tasks to see if this agent already holds one in_progress
            for other_id, other_task in self._task_cache.get("tasks", {}).items():
                if other_id == task_id:
                    continue  # skip the task we're trying to claim
                other_agent = other_task.get("agent")
                if (
                    other_task.get("status") == "in_progress"
                    and other_agent
                    and other_agent.get("claimed_by") == agent_id
                    and not self._is_abandoned(other_task, ttl)
                ):
                    raise PermissionError(
                        f"Agent {agent_id!r} already holds {other_id} in_progress. "
                        f"Complete or release it first."
                    )

            # ── Claimable check ──────────────────────────────────────────
            status = task.get("status", "")
            current_agent = task.get("agent")
            abandoned = self._is_abandoned(task, ttl)

            if status in CLAIMABLE_STATUSES:
                # Normal claim: task is open or on_hold
                action = "claimed"
                detail = ""
            elif status == "in_progress" and abandoned:
                # Reclaim: previous agent's heartbeat expired
                old_agent = current_agent.get("claimed_by", "unknown") if current_agent else "unknown"
                action = "reclaimed"
                detail = f"Previous agent {old_agent!r} abandoned (heartbeat expired)"
                logger.warning(
                    "Task %s reclaimed from %s by %s (heartbeat expired)",
                    task_id, old_agent, agent_id,
                )
            elif status == "in_progress":
                # Task is actively held by another agent
                holder = current_agent.get("claimed_by", "unknown") if current_agent else "unknown"
                raise PermissionError(
                    f"Task {task_id!r} is in_progress, held by {holder!r}. "
                    f"Wait for release or heartbeat expiry."
                )
            elif status == "done":
                raise ValueError(f"Task {task_id!r} is already done. Cannot claim.")
            else:
                raise ValueError(f"Task {task_id!r} has unexpected status: {status!r}")

            # ── Set ownership ────────────────────────────────────────────
            now = self._now()
            task["status"] = "in_progress"
            task["updated_at"] = now
            task["agent"] = {
                "claimed_by": agent_id,
                "claimed_at": now,
                "host": host,
                "model": model,
                "pid": pid,
                "heartbeat": now,
                "heartbeat_ttl_minutes": ttl,
                "current_phase": "",
                "current_action": "",
                "files_touched": [],
                "progress_pct": 0,
                "blockers": [],
            }

            self._append_history(task, agent_id, host, action, detail)
            self._increment_version()
            self._flush_local()

            result = dict(task)
            result["id"] = task_id
            return result
```


## 3A-12. update_task

```
    def update_task(
        self,
        task_id: str,
        agent_id: str,
        host: str,
        notes: str = "",
        phase: str | None = None,
        action: str | None = None,
        progress: int | None = None,
        files: list[str] | None = None,
        blockers: list[str] | None = None,
        criteria_done: list[int] | None = None,
    ) -> dict:
        """Update progress on a claimed task. Owner-only.

        Security:
        - claimed_by must match agent_id. This is the ownership check.
          agent_id comes from the server dispatch layer (not spoofable).
        - Heartbeat is refreshed on every update call. This is how the
          server knows the agent is still alive.
        - progress is clamped to 0-100.
        - criteria_done marks specific acceptance criteria IDs as done.
          Only IDs that exist in the criteria list are accepted (no injection
          of phantom criteria).

        This is the most-called method during active work. Keep it efficient.
        """
        with self._task_lock:
            task = self._task_cache.get("tasks", {}).get(task_id)
            if task is None:
                raise KeyError(f"Task {task_id!r} not found")

            # ── Ownership check ──────────────────────────────────────────
            current_agent = task.get("agent")
            if not current_agent or current_agent.get("claimed_by") != agent_id:
                raise PermissionError(
                    f"Agent {agent_id!r} does not own task {task_id!r}. "
                    f"Claimed by: {current_agent.get('claimed_by') if current_agent else 'nobody'}"
                )

            if task.get("status") != "in_progress":
                raise ValueError(f"Task {task_id!r} is not in_progress (status: {task.get('status')})")

            now = self._now()

            # ── Refresh heartbeat (always, even if no other changes) ─────
            current_agent["heartbeat"] = now
            task["updated_at"] = now

            # ── Apply optional field updates ─────────────────────────────
            changes = []

            if phase is not None:
                current_agent["current_phase"] = str(phase)
                changes.append(f"phase={phase}")

            if action is not None:
                current_agent["current_action"] = str(action)
                changes.append(f"action={action}")

            if progress is not None:
                # Clamp to valid range
                clamped = max(0, min(100, int(progress)))
                current_agent["progress_pct"] = clamped
                changes.append(f"progress={clamped}%")

            if files is not None:
                # Merge with existing files_touched (dedup, preserve order)
                existing = current_agent.get("files_touched", [])
                seen = set(existing)
                for f in files:
                    if f not in seen:
                        existing.append(f)
                        seen.add(f)
                current_agent["files_touched"] = existing
                changes.append(f"files=+{len(files)}")

            if blockers is not None:
                current_agent["blockers"] = list(blockers)
                changes.append(f"blockers={len(blockers)}")

            if criteria_done is not None:
                # Mark specific acceptance criteria as done
                # Security: only IDs that actually exist in the list are updated
                valid_ids = {ac["id"] for ac in task.get("acceptance_criteria", [])}
                applied = []
                for cid in criteria_done:
                    if cid not in valid_ids:
                        continue  # silently skip invalid IDs (no error leaking schema)
                    for ac in task["acceptance_criteria"]:
                        if ac["id"] == cid:
                            ac["done"] = True
                            applied.append(cid)
                            break
                if applied:
                    changes.append(f"criteria_done={applied}")

            # ── History entry ────────────────────────────────────────────
            detail_parts = []
            if notes:
                detail_parts.append(notes)
            if changes:
                detail_parts.append("; ".join(changes))
            detail = " | ".join(detail_parts) if detail_parts else "heartbeat"

            self._append_history(task, agent_id, host, "progress", detail)
            self._increment_version()
            self._flush_local()

            result = dict(task)
            result["id"] = task_id
            return result
```


## 3A-13. complete_task

```
    def complete_task(
        self,
        task_id: str,
        agent_id: str,
        host: str,
        result: str = "",
    ) -> dict:
        """Mark a claimed task as done. Owner-only.

        Security:
        - Ownership check: claimed_by must match agent_id.
        - Agent block is wiped on completion (no stale claims).
        - Status set to "done", completed_at timestamped.
        - Result is stored for post-completion review.
        - History records the completion with the result summary.
        """
        with self._task_lock:
            task = self._task_cache.get("tasks", {}).get(task_id)
            if task is None:
                raise KeyError(f"Task {task_id!r} not found")

            # ── Ownership check ──────────────────────────────────────────
            current_agent = task.get("agent")
            if not current_agent or current_agent.get("claimed_by") != agent_id:
                raise PermissionError(
                    f"Agent {agent_id!r} does not own task {task_id!r}"
                )

            if task.get("status") != "in_progress":
                raise ValueError(f"Task {task_id!r} is not in_progress")

            now = self._now()

            # ── Completion ───────────────────────────────────────────────
            task["status"] = "done"
            task["completed_at"] = now
            task["updated_at"] = now
            task["result"] = result

            # Wipe agent block — no stale ownership after completion
            # The history preserves who completed it; the agent block is mutable state.
            task["agent"] = None

            detail = f"Result: {result[:200]}" if result else "Completed"
            self._append_history(task, agent_id, host, "completed", detail)
            self._increment_version()
            self._flush_local()

            out = dict(task)
            out["id"] = task_id
            return out
```


## 3A-14. release_task

```
    def release_task(
        self,
        task_id: str,
        agent_id: str,
        host: str,
        reason: str = "",
    ) -> dict:
        """Release a claimed task back to open. Owner-only.

        Use cases:
        - Agent realizes it can't complete the task (wrong skills, blocked)
        - Agent is shutting down gracefully
        - Agent wants to hand off to another agent

        Security:
        - Ownership check: claimed_by must match agent_id.
        - Status goes back to "open" (not on_hold — that's a separate state).
        - Agent block is wiped.
        - Reason is recorded in history for audit.
        """
        with self._task_lock:
            task = self._task_cache.get("tasks", {}).get(task_id)
            if task is None:
                raise KeyError(f"Task {task_id!r} not found")

            # ── Ownership check ──────────────────────────────────────────
            current_agent = task.get("agent")
            if not current_agent or current_agent.get("claimed_by") != agent_id:
                raise PermissionError(
                    f"Agent {agent_id!r} does not own task {task_id!r}"
                )

            if task.get("status") != "in_progress":
                raise ValueError(f"Task {task_id!r} is not in_progress")

            now = self._now()

            # ── Release ──────────────────────────────────────────────────
            task["status"] = "open"
            task["updated_at"] = now

            # Wipe agent block
            task["agent"] = None

            detail = f"Reason: {reason}" if reason else "Released without reason"
            self._append_history(task, agent_id, host, "released", detail)
            self._increment_version()
            self._flush_local()

            out = dict(task)
            out["id"] = task_id
            return out
```


## 3A-15. version property (for GitLab sync)

```
    @property
    def version(self) -> int:
        """Current monotonic version. Used by GitLab sync thread (Phase 3c).

        The sync thread reads this to decide whether local has new mutations
        to push. Thread-safe: single int read is atomic on CPython, but we
        still use the lock for correctness on other runtimes.
        """
        with self._task_lock:
            return self._task_cache.get("version", 0)

    @property
    def task_cache_snapshot(self) -> dict:
        """Deep-enough copy of task cache for GitLab sync thread.

        The sync thread needs the full state to serialize to GitLab.
        We return a JSON-round-tripped copy to avoid shared mutable state.

        ONLY called by the sync thread (Phase 3c). Not exposed via MCP.
        """
        with self._task_lock:
            return json.loads(json.dumps(self._task_cache))

    def merge_remote(self, remote_data: dict) -> None:
        """Merge remotely-updated task data into local cache.

        Called by the GitLab sync thread (Phase 3c) when remote has
        changes not present locally. Merge strategy:
        - Remote version must be > local version (or skip)
        - New tasks from remote are added
        - Existing tasks: remote wins UNLESS local has an active claim
          (in_progress with live heartbeat) — active work is never overwritten

        This method is a placeholder signature — full implementation in Phase 3c.
        """
        with self._task_lock:
            pass  # Phase 3c
```


## Security Summary

| Concern | Mitigation |
|---|---|
| Race conditions on claim | `_task_lock` serializes all reads and writes |
| Agent spoofing | `agent_id` set by server dispatch, not by caller |
| Task hoarding | One-at-a-time check scans all tasks before allowing claim |
| Stale claims | Heartbeat TTL check enables reclaim of abandoned tasks |
| TTL manipulation | TTL comes from server config, not from task/agent data |
| Unauthorized create | `TASK_CREATE_ALLOWED` prefix list restricts who can create |
| Unauthorized mutation | Ownership check (claimed_by == agent_id) on update/complete/release |
| History tampering | History is append-only; entries use server-set agent_id and host |
| Torn reads on disk | Atomic write via os.replace() from .tmp file |
| Torn reads in memory | All reads acquire _task_lock (consistent snapshot) |
| Version monotonicity | Version incremented on every mutation; used by sync for conflict detection |
| Phantom criteria injection | criteria_done only applies to IDs that exist in the task's criteria list |
