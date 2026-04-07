"""Local-first task engine with thread-safe mutable operations.

All public methods are serialized by _task_lock.
agent_id is set by the server dispatch layer — callers cannot spoof it.

Security:
- Race conditions on claim: _task_lock serializes all reads and writes
- Agent spoofing: agent_id set by server dispatch, not by caller
- Task hoarding: one-at-a-time check scans all tasks before allowing claim
- Stale claims: heartbeat TTL check enables reclaim of abandoned tasks
- TTL manipulation: TTL comes from server config, not from task/agent data
- Unauthorized create: TASK_CREATE_ALLOWED prefix list restricts who can create
- Unauthorized mutation: ownership check on update/complete/release
- History tampering: history is append-only with server-set agent_id and host
- Torn reads on disk: atomic write via os.replace() from .tmp file
- Torn reads in memory: all reads acquire _task_lock
"""

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from vaire.config import Settings

logger = logging.getLogger(__name__)

VALID_STATUSES = {"open", "in_progress", "done", "on_hold"}
VALID_PRIORITIES = {"critical", "high", "medium", "low"}
CLAIMABLE_STATUSES = {"open", "on_hold"}

_EMPTY_STORE = {
    "schema_version": 1,
    "version": 0,
    "next_id": 1,
    "tasks": {},
}


class TaskEngine:
    """Local-first task cache with thread-safe mutable operations."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._task_lock = threading.Lock()
        self._task_cache: dict = {}
        self._dirty: list[bool] = [False]
        self._data_path: Path = settings.task_data_path_resolved
        self._load_or_seed()

    def _load_or_seed(self) -> None:
        """Load task state from disk or create empty skeleton."""
        if self._data_path.exists():
            try:
                raw = json.loads(self._data_path.read_text())
                if raw.get("schema_version") != 1:
                    raise ValueError(
                        f"Unknown schema_version: {raw.get('schema_version')}"
                    )
                if not all(
                    k in raw for k in ("tasks", "version", "next_id")
                ):
                    raise ValueError("Missing required top-level keys")
                self._task_cache = raw
                logger.info(
                    "TaskEngine loaded %d tasks from %s (version %d)",
                    len(raw["tasks"]), self._data_path, raw["version"],
                )
                return
            except (json.JSONDecodeError, ValueError, KeyError) as exc:
                logger.error(
                    "Failed to load %s: %s — creating empty store",
                    self._data_path, exc,
                )

        # Empty skeleton
        self._task_cache = dict(_EMPTY_STORE)
        self._task_cache["tasks"] = {}  # fresh dict
        self._flush_local()
        logger.info("TaskEngine initialized with empty store")

    def _flush_local(self) -> None:
        """Write _task_cache to disk atomically.

        MUST be called under _task_lock.
        """
        tmp_path = self._data_path.with_suffix(".json.tmp")
        self._data_path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(self._task_cache, indent=2, sort_keys=False)
        tmp_path.write_text(content)
        os.replace(str(tmp_path), str(self._data_path))

    def _mark_dirty(self) -> None:
        """Flag cache as modified. MUST be called under _task_lock."""
        self._dirty[0] = True

    @staticmethod
    def _now() -> str:
        """ISO 8601 UTC timestamp, timezone-aware."""
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _append_history(
        task: dict, agent_id: str, host: str, action: str, detail: str = "",
    ) -> None:
        """Append an entry to the task's history list (append-only)."""
        entry: dict = {
            "ts": TaskEngine._now(),
            "by": agent_id,
            "host": host,
            "action": action,
        }
        if detail:
            entry["detail"] = detail
        task.setdefault("history", []).append(entry)

    def _increment_version(self) -> None:
        """Bump monotonic version counter. MUST be called under _task_lock."""
        self._task_cache["version"] = self._task_cache.get("version", 0) + 1

    @staticmethod
    def _is_abandoned(task: dict, ttl_minutes: int) -> bool:
        """Check if a claimed task's heartbeat has expired."""
        agent = task.get("agent")
        if not agent or not agent.get("claimed_by"):
            return False

        heartbeat = agent.get("heartbeat") or agent.get("claimed_at")
        if not heartbeat:
            return True

        try:
            hb_dt = datetime.fromisoformat(heartbeat)
            # Ensure timezone-aware comparison
            if hb_dt.tzinfo is None:
                hb_dt = hb_dt.replace(tzinfo=timezone.utc)
            elapsed = datetime.now(timezone.utc) - hb_dt
            return elapsed.total_seconds() > (ttl_minutes * 60)
        except (ValueError, TypeError):
            return True

    def list_tasks(
        self,
        status: str | None = None,
        role: str | None = None,
        include_history: bool = False,
    ) -> list[dict]:
        """Filtered read of all tasks. Thread-safe."""
        with self._task_lock:
            ttl = self._settings.TASK_HEARTBEAT_TTL
            results = []
            for task_id, task in self._task_cache.get("tasks", {}).items():
                if status and task.get("status") != status:
                    continue
                if role and task.get("role") != role:
                    continue

                out = {
                    "id": task_id,
                    "title": task.get("title", ""),
                    "status": task.get("status", ""),
                    "priority": task.get("priority", "medium"),
                    "role": task.get("role", ""),
                    "directory": task.get("directory", ""),
                    "description": task.get("description", ""),
                    "depends_on": list(task.get("depends_on", [])),
                    "acceptance_criteria": json.loads(
                        json.dumps(task.get("acceptance_criteria", []))
                    ),
                    "context_queries": list(task.get("context_queries", [])),
                    "on_completion": task.get("on_completion"),
                    "created_at": task.get("created_at", ""),
                    "created_by": task.get("created_by", ""),
                    "updated_at": task.get("updated_at", ""),
                    "completed_at": task.get("completed_at"),
                    "result": task.get("result"),
                    "_abandoned": self._is_abandoned(task, ttl),
                }

                agent = task.get("agent")
                if agent:
                    out["agent"] = json.loads(json.dumps(agent))

                if include_history:
                    out["history"] = json.loads(
                        json.dumps(task.get("history", []))
                    )

                results.append(out)

            return results

    def get_task(self, task_id: str) -> dict | None:
        """Single task read by ID. Returns full deep copy or None."""
        with self._task_lock:
            task = self._task_cache.get("tasks", {}).get(task_id)
            if task is None:
                return None

            ttl = self._settings.TASK_HEARTBEAT_TTL
            out = json.loads(json.dumps(task))
            out["id"] = task_id
            out["_abandoned"] = self._is_abandoned(task, ttl)
            return out

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

        If TASK_CREATE_ALLOWED is empty, all agents can create tasks.
        """
        allowed_prefixes = self._settings.task_create_allowed_list
        if allowed_prefixes:
            if not any(
                agent_id.startswith(prefix) for prefix in allowed_prefixes
            ):
                raise PermissionError(
                    f"Agent {agent_id!r} not authorized to create tasks. "
                    f"Allowed prefixes: {allowed_prefixes}"
                )

        if not title or not title.strip():
            raise ValueError("Task title is required")
        if priority not in VALID_PRIORITIES:
            raise ValueError(
                f"Invalid priority {priority!r}. "
                f"Must be one of: {VALID_PRIORITIES}"
            )

        criteria = []
        if acceptance_criteria:
            for i, ac in enumerate(acceptance_criteria):
                if not isinstance(ac, dict) or "text" not in ac:
                    raise ValueError(
                        f"acceptance_criteria[{i}] must have 'text' key"
                    )
                criteria.append({
                    "id": ac.get("id", i + 1),
                    "text": str(ac["text"]),
                    "done": bool(ac.get("done", False)),
                })

        now = self._now()

        with self._task_lock:
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
                "depends_on": list(depends_on or []),
                "context_queries": list(context_queries or []),
                "on_completion": on_completion,
                "created_at": now,
                "created_by": agent_id,
                "updated_at": now,
                "agent": None,
                "history": [],
                "completed_at": None,
                "result": None,
            }

            self._append_history(
                task, agent_id, host, "created",
                f"Priority: {priority}, Role: {role}",
            )

            self._task_cache["tasks"][task_id] = task
            self._increment_version()
            self._mark_dirty()
            self._flush_local()

            result = json.loads(json.dumps(task))
            result["id"] = task_id
            return result

    def claim_task(
        self,
        task_id: str,
        agent_id: str,
        host: str,
        model: str = "",
        pid: int = 0,
    ) -> dict:
        """Claim an open or abandoned task. One-at-a-time enforcement."""
        with self._task_lock:
            task = self._task_cache.get("tasks", {}).get(task_id)
            if task is None:
                raise KeyError(f"Task {task_id!r} not found")

            ttl = self._settings.TASK_HEARTBEAT_TTL

            # One-at-a-time: agent cannot hold two in_progress tasks
            for other_id, other_task in self._task_cache.get(
                "tasks", {}
            ).items():
                if other_id == task_id:
                    continue
                other_agent = other_task.get("agent")
                if (
                    other_task.get("status") == "in_progress"
                    and other_agent
                    and other_agent.get("claimed_by") == agent_id
                    and not self._is_abandoned(other_task, ttl)
                ):
                    raise PermissionError(
                        f"Agent {agent_id!r} already holds {other_id} "
                        f"in_progress. Complete or release it first."
                    )

            status = task.get("status", "")
            current_agent = task.get("agent")
            abandoned = self._is_abandoned(task, ttl)

            if status in CLAIMABLE_STATUSES:
                action = "claimed"
                detail = ""
            elif status == "in_progress" and abandoned:
                old_agent = (
                    current_agent.get("claimed_by", "unknown")
                    if current_agent else "unknown"
                )
                action = "reclaimed"
                detail = (
                    f"Previous agent {old_agent!r} abandoned "
                    "(heartbeat expired)"
                )
                logger.warning(
                    "Task %s reclaimed from %s by %s (heartbeat expired)",
                    task_id, old_agent, agent_id,
                )
            elif status == "in_progress":
                holder = (
                    current_agent.get("claimed_by", "unknown")
                    if current_agent else "unknown"
                )
                raise PermissionError(
                    f"Task {task_id!r} is in_progress, held by {holder!r}. "
                    f"Wait for release or heartbeat expiry."
                )
            elif status == "done":
                raise ValueError(
                    f"Task {task_id!r} is already done. Cannot claim."
                )
            else:
                raise ValueError(
                    f"Task {task_id!r} has unexpected status: {status!r}"
                )

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
            self._mark_dirty()
            self._flush_local()

            result = json.loads(json.dumps(task))
            result["id"] = task_id
            return result

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
        """Update progress on a claimed task. Owner-only."""
        with self._task_lock:
            task = self._task_cache.get("tasks", {}).get(task_id)
            if task is None:
                raise KeyError(f"Task {task_id!r} not found")

            current_agent = task.get("agent")
            if not current_agent or current_agent.get(
                "claimed_by"
            ) != agent_id:
                raise PermissionError(
                    f"Agent {agent_id!r} does not own task {task_id!r}. "
                    f"Claimed by: "
                    f"{current_agent.get('claimed_by') if current_agent else 'nobody'}"
                )

            if task.get("status") != "in_progress":
                raise ValueError(
                    f"Task {task_id!r} is not in_progress "
                    f"(status: {task.get('status')})"
                )

            now = self._now()
            current_agent["heartbeat"] = now
            task["updated_at"] = now

            changes = []

            if phase is not None:
                current_agent["current_phase"] = str(phase)
                changes.append(f"phase={phase}")

            if action is not None:
                current_agent["current_action"] = str(action)
                changes.append(f"action={action}")

            if progress is not None:
                clamped = max(0, min(100, int(progress)))
                current_agent["progress_pct"] = clamped
                changes.append(f"progress={clamped}%")

            if files is not None:
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
                valid_ids = {
                    ac["id"]
                    for ac in task.get("acceptance_criteria", [])
                }
                applied = []
                for cid in criteria_done:
                    if cid not in valid_ids:
                        continue
                    for ac in task["acceptance_criteria"]:
                        if ac["id"] == cid:
                            ac["done"] = True
                            applied.append(cid)
                            break
                if applied:
                    changes.append(f"criteria_done={applied}")

            detail_parts = []
            if notes:
                detail_parts.append(notes)
            if changes:
                detail_parts.append("; ".join(changes))
            detail = " | ".join(detail_parts) if detail_parts else "heartbeat"

            self._append_history(task, agent_id, host, "progress", detail)
            self._increment_version()
            self._mark_dirty()
            self._flush_local()

            result = json.loads(json.dumps(task))
            result["id"] = task_id
            return result

    def complete_task(
        self,
        task_id: str,
        agent_id: str,
        host: str,
        result: str = "",
    ) -> dict:
        """Mark a claimed task as done. Owner-only."""
        with self._task_lock:
            task = self._task_cache.get("tasks", {}).get(task_id)
            if task is None:
                raise KeyError(f"Task {task_id!r} not found")

            current_agent = task.get("agent")
            if not current_agent or current_agent.get(
                "claimed_by"
            ) != agent_id:
                raise PermissionError(
                    f"Agent {agent_id!r} does not own task {task_id!r}"
                )

            if task.get("status") != "in_progress":
                raise ValueError(
                    f"Task {task_id!r} is not in_progress"
                )

            now = self._now()
            task["status"] = "done"
            task["completed_at"] = now
            task["updated_at"] = now
            task["result"] = result
            task["agent"] = None

            detail = f"Result: {result[:200]}" if result else "Completed"
            self._append_history(task, agent_id, host, "completed", detail)
            self._increment_version()
            self._mark_dirty()
            self._flush_local()

            out = json.loads(json.dumps(task))
            out["id"] = task_id
            return out

    def release_task(
        self,
        task_id: str,
        agent_id: str,
        host: str,
        reason: str = "",
    ) -> dict:
        """Release a claimed task back to open. Owner-only."""
        with self._task_lock:
            task = self._task_cache.get("tasks", {}).get(task_id)
            if task is None:
                raise KeyError(f"Task {task_id!r} not found")

            current_agent = task.get("agent")
            if not current_agent or current_agent.get(
                "claimed_by"
            ) != agent_id:
                raise PermissionError(
                    f"Agent {agent_id!r} does not own task {task_id!r}"
                )

            if task.get("status") != "in_progress":
                raise ValueError(
                    f"Task {task_id!r} is not in_progress"
                )

            now = self._now()
            task["status"] = "open"
            task["updated_at"] = now
            task["agent"] = None

            detail = f"Reason: {reason}" if reason else "Released without reason"
            self._append_history(task, agent_id, host, "released", detail)
            self._increment_version()
            self._mark_dirty()
            self._flush_local()

            out = json.loads(json.dumps(task))
            out["id"] = task_id
            return out

    @property
    def version(self) -> int:
        """Current monotonic version."""
        with self._task_lock:
            return self._task_cache.get("version", 0)

    @property
    def task_cache_snapshot(self) -> dict:
        """Deep copy of task cache for sync thread."""
        with self._task_lock:
            return json.loads(json.dumps(self._task_cache))

    def merge_remote(self, remote_data: dict) -> None:
        """Merge remotely-updated task data. Used by TaskSyncThread."""
        with self._task_lock:
            self._task_cache.update(remote_data)
            self._flush_local()


# ── GitLab sync thread ───────────────────────────────────────────────────


class TaskSyncThread:
    """Background thread that syncs local task cache to GitLab every N seconds.

    Protocol:
    1. Acquire lock, check dirty, snapshot cache, release lock
    2. Outside lock: read GitLab, merge, push with optimistic locking
    3. On 409: re-read, re-merge, retry (max 3)
    4. On unreachable: dirty stays True, retry next cycle
    5. On success: clear dirty flag

    Security:
    - Token NEVER in commit messages, logs, or errors
    - Commit messages contain only "vaire-task-sync:" + task IDs
    - Version monotonicity checked — tamper detection
    """

    MAX_RETRIES = 3

    def __init__(self, task_engine: TaskEngine, gitlab, settings):
        self._engine = task_engine
        self._lock = task_engine._task_lock
        self._dirty = task_engine._dirty  # mutable list[bool] ref
        self._gitlab = gitlab
        self._interval = settings.TASK_SYNC_INTERVAL
        self._file_path = settings.GITLAB_TASKS_FILE
        self._branch = settings.GITLAB_TASKS_BRANCH
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_commit_id: str | None = None
        self._logger = logging.getLogger("vaire.task_sync")

    def start(self) -> None:
        """Spawn daemon thread. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._sync_loop,
            name="vaire-task-sync",
            daemon=True,
        )
        self._thread.start()
        self._logger.info(
            "Task sync thread started (interval=%ds)", self._interval
        )

    def stop(self) -> None:
        """Signal stop, attempt final sync, join with timeout."""
        self._stop_event.set()
        try:
            self._sync_once()
        except Exception:
            self._logger.warning(
                "Final sync failed during shutdown", exc_info=True
            )
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                self._logger.warning(
                    "Task sync thread did not exit within 5s"
                )
            self._thread = None

    def _sync_loop(self) -> None:
        """Main loop: startup merge, then periodic sync."""
        first_run = True
        while not self._stop_event.is_set():
            try:
                if first_run:
                    self._startup_merge()
                    first_run = False
                else:
                    self._sync_once()
            except Exception:
                self._logger.error("Sync cycle failed", exc_info=True)
            if self._stop_event.wait(timeout=self._interval):
                break

    def _startup_merge(self) -> None:
        """On startup: merge local state with remote."""
        with self._lock:
            local = json.loads(json.dumps(self._engine._task_cache))
            has_local = bool(local.get("tasks"))

        if not has_local:
            # No local state — pull from GitLab to seed
            try:
                content, commit_id = self._gitlab.read_file(
                    self._file_path, self._branch
                )
                remote = json.loads(content)
                self._validate_schema(remote)
                with self._lock:
                    self._engine._task_cache.update(remote)
                    self._dirty[0] = False
                self._last_commit_id = commit_id
                self._logger.info("Startup: seeded from GitLab")
            except FileNotFoundError:
                self._logger.info("Startup: no remote — starting fresh")
            except Exception:
                self._logger.warning(
                    "Startup: GitLab unreachable", exc_info=True
                )
            return

        # Local exists — merge with remote
        try:
            content, commit_id = self._gitlab.read_file(
                self._file_path, self._branch
            )
            remote = json.loads(content)
            self._validate_schema(remote)
            self._last_commit_id = commit_id
            merged = self._merge(local, remote)
            self._push_with_retry(merged)
        except FileNotFoundError:
            self._push_with_retry(local)
        except Exception:
            self._logger.warning(
                "Startup merge failed — will retry", exc_info=True
            )
            with self._lock:
                self._dirty[0] = True

    def _sync_once(self) -> None:
        """Single sync cycle."""
        from vaire.gitlab_client import GitLabError

        with self._lock:
            if not self._dirty[0]:
                return
            local = json.loads(json.dumps(self._engine._task_cache))

        try:
            content, commit_id = self._gitlab.read_file(
                self._file_path, self._branch
            )
            remote = json.loads(content)
            self._validate_schema(remote)
            self._last_commit_id = commit_id
        except FileNotFoundError:
            remote = None
        except (GitLabError, ConnectionError, json.JSONDecodeError):
            self._logger.warning(
                "GitLab unreachable — retry next cycle"
            )
            return  # dirty stays True

        merged = self._merge(local, remote) if remote else local
        self._push_with_retry(merged)

    def _push_with_retry(self, merged: dict) -> None:
        """Push merged state to GitLab with retry on 409."""
        from vaire.gitlab_client import GitLabConflictError, GitLabError

        for attempt in range(self.MAX_RETRIES):
            try:
                modified = self._diff_task_ids(merged)
                summary = (
                    f"{len(modified)} tasks"
                    if len(modified) > 5
                    else ",".join(modified) or "sync"
                )
                commit_msg = f"vaire-task-sync: {summary}"

                content_str = json.dumps(
                    merged, indent=2, sort_keys=True
                )
                new_commit = self._gitlab.write_file(
                    self._file_path,
                    content_str,
                    commit_msg,
                    self._branch,
                    self._last_commit_id,
                )
                self._last_commit_id = new_commit

                with self._lock:
                    self._dirty[0] = False

                self._logger.info(
                    "Sync pushed: %s (commit=%s)",
                    summary, new_commit[:8] if new_commit else "?",
                )
                return

            except GitLabConflictError:
                self._logger.info(
                    "Conflict on attempt %d/%d — re-merging",
                    attempt + 1, self.MAX_RETRIES,
                )
                if attempt + 1 >= self.MAX_RETRIES:
                    self._logger.error(
                        "Max retries on 409 — will retry next cycle"
                    )
                    return

                try:
                    content, commit_id = self._gitlab.read_file(
                        self._file_path, self._branch
                    )
                    remote = json.loads(content)
                    self._validate_schema(remote)
                    self._last_commit_id = commit_id
                except Exception:
                    self._logger.warning(
                        "Re-read after conflict failed"
                    )
                    return

                with self._lock:
                    local = json.loads(
                        json.dumps(self._engine._task_cache)
                    )
                merged = self._merge(local, remote)

            except (GitLabError, ConnectionError):
                self._logger.warning(
                    "GitLab write failed — retry next cycle"
                )
                return

    def _merge(self, local: dict, remote: dict) -> dict:
        """Merge local and remote task state.

        - Exists only in local/remote: include as-is
        - Both exist: field-level merge
          - title/description/priority/role: remote wins (creator edits)
          - status: done is terminal, else more-progressed wins
          - agent: newer heartbeat wins
          - history: union, dedupe by (ts, action, by)
          - acceptance_criteria: local wins (agent may have checked boxes)
        """
        STATUS_ORDER = {
            "open": 0, "on_hold": 1, "in_progress": 2, "done": 3,
        }

        merged: dict = {
            "schema_version": local.get("schema_version", 1),
            "version": max(
                local.get("version", 0), remote.get("version", 0)
            ) + 1,
            "next_id": max(
                local.get("next_id", 1), remote.get("next_id", 1)
            ),
            "tasks": {},
        }

        # Version monotonicity check
        if (
            remote.get("version", 0) < local.get("version", 0)
            and remote.get("tasks")
        ):
            self._logger.warning(
                "SECURITY: remote version (%d) < local (%d) "
                "— possible tampering or rollback",
                remote.get("version", 0), local.get("version", 0),
            )

        all_ids = (
            set(local.get("tasks", {}).keys())
            | set(remote.get("tasks", {}).keys())
        )

        for tid in all_ids:
            lt = local.get("tasks", {}).get(tid)
            rt = remote.get("tasks", {}).get(tid)

            if lt is None:
                merged["tasks"][tid] = json.loads(json.dumps(rt))
                continue
            if rt is None:
                merged["tasks"][tid] = json.loads(json.dumps(lt))
                continue

            # Both exist — field-level merge
            m = json.loads(json.dumps(lt))  # start from local

            # Creator-editable fields: remote wins
            for field in ("title", "description", "priority", "role"):
                if field in rt:
                    m[field] = rt[field]

            # Status: done is terminal, else more-progressed wins
            ls = lt.get("status", "open")
            rs = rt.get("status", "open")
            if ls == "done" or rs == "done":
                m["status"] = "done"
            else:
                l_ord = STATUS_ORDER.get(ls, 0)
                r_ord = STATUS_ORDER.get(rs, 0)
                m["status"] = ls if l_ord >= r_ord else rs

            # Agent block: newer heartbeat wins
            la = lt.get("agent") or {}
            ra = rt.get("agent") or {}
            lhb = la.get("heartbeat", "")
            rhb = ra.get("heartbeat", "")
            if rhb > lhb:
                m["agent"] = json.loads(json.dumps(ra)) if ra else None

            # History: union, dedupe by (ts, action, by)
            lh = lt.get("history", [])
            rh = rt.get("history", [])
            seen: set = set()
            merged_history = []
            for entry in lh + rh:
                key = (
                    entry.get("ts", ""),
                    entry.get("action", ""),
                    entry.get("by", ""),
                )
                if key not in seen:
                    seen.add(key)
                    merged_history.append(entry)
            merged_history.sort(key=lambda e: e.get("ts", ""))
            m["history"] = merged_history

            # acceptance_criteria: local wins (already in m)

            merged["tasks"][tid] = m

        return merged

    def _validate_schema(self, remote: dict) -> None:
        """Refuse to merge if schema_version differs."""
        remote_sv = remote.get("schema_version", 1)
        if remote_sv != 1:
            self._logger.error(
                "Schema mismatch: local=1 remote=%d", remote_sv
            )
            raise ValueError(
                f"Task schema_version mismatch: remote={remote_sv}"
            )

    def _diff_task_ids(self, merged: dict) -> list[str]:
        """Task IDs that differ — for commit messages only."""
        with self._lock:
            current = set(
                self._engine._task_cache.get("tasks", {}).keys()
            )
        merged_ids = set(merged.get("tasks", {}).keys())
        changed = merged_ids ^ current
        return sorted(changed) if len(changed) <= 10 else sorted(
            merged_ids
        )
