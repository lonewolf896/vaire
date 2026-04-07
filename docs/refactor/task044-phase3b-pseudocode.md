# TASK-044 Phase 3B: MCP Tool Registration + Dispatch + Permission Model

## Overview

Register 7 task MCP tools via `@mcp_server.tool()`, build a task dispatch table
for the Unix socket server, wire TaskEngine init into `init_engines`, and enforce
the permission model (local-only mutations, remote read-only, create-prefix gating).

All changes are in `vaire/server.py` and `vaire/__main__.py`.


## 3B-1. Global instance (vaire/server.py)

Add to the globals block (after `_pipeline = None`):

```
_task_engine = None  # TaskEngine — imported lazily in init_engines
```


## 3B-2. init_engines addition (vaire/server.py)

Inside `init_engines()`, after existing engine initialization, add task engine init
wrapped in try/except so a failure does NOT prevent the server from starting:

```
# ── Task engine (non-fatal) ───────────────────────────────────────
global _task_engine
try:
    from vaire.task_engine import TaskEngine
    _task_engine = TaskEngine(_settings)
    logger.info("TaskEngine initialized (data=%s)", _settings.task_data_path_resolved)
except Exception:
    logger.warning("TaskEngine init failed — task tools will return errors", exc_info=True)
    _task_engine = None
```

No shutdown hook needed — TaskEngine has no background threads (sync thread is Phase 3C).


## 3B-3. Helper: guard for uninitialized task engine

```
def _require_task_engine() -> "TaskEngine":
    """Return the TaskEngine or raise a user-friendly error."""
    if _task_engine is None:
        raise RuntimeError("Task system is not available — check server logs")
    return _task_engine
```


## 3B-4. MCP tool functions (vaire/server.py)

Seven tools registered via `@mcp_server.tool()`. Each:
- Calls `_require_task_engine()` first
- Checks permission model (remote block, create-prefix gate)
- Delegates to TaskEngine method
- Returns dict (matching Vaire return conventions)

### task_list

```
@mcp_server.tool()
def task_list(status: str = "", role: str = "", include_history: bool = False) -> dict:
    """List tasks, optionally filtered by status and/or role.

    Args:
        status: Filter by status (open, claimed, done, blocked, abandoned). Empty = all.
        role: Filter by assigned role. Empty = all roles.
        include_history: If True, include completed/abandoned tasks in results.
    """
    engine = _require_task_engine()
    return engine.list_tasks(status=status, role=role, include_history=include_history)
```

### task_get

```
@mcp_server.tool()
def task_get(task_id: str) -> dict:
    """Get full details for a single task including acceptance criteria and history.

    Args:
        task_id: The task identifier (e.g. "TASK-044").
    """
    engine = _require_task_engine()
    result = engine.get_task(task_id)
    if result is None:
        return {"status": "error", "message": f"Task not found: {task_id}"}
    return result
```

### task_create

```
@mcp_server.tool()
def task_create(
    title: str,
    role: str,
    priority: str = "medium",
    directory: str = "",
    description: str = "",
    acceptance_criteria: list[str] = [],
    depends_on: list[str] = [],
    context_queries: list[str] = [],
    on_completion: str = "",
) -> dict:
    """Create a new task. Restricted to allowed agent prefixes (TASK_CREATE_ALLOWED).

    Args:
        title: Short title for the task.
        role: Target role (e.g. "builder", "defender", "architect").
        priority: One of "low", "medium", "high", "critical". Default "medium".
        directory: Project directory scope (optional).
        description: Longer description of the task.
        acceptance_criteria: List of criteria that must be met to complete the task.
        depends_on: List of task IDs this task depends on.
        context_queries: Vaire recall queries to run when claiming this task.
        on_completion: Action to take on completion (e.g. recall query to run).
    """
    # ── Block remote creation ──
    if transport_ctx.get().is_remote:
        return {"status": "error", "message": "Task creation not permitted via remote transport"}

    # ── Prefix-based create permission ──
    # agent_id is not available inside @mcp_server.tool() — this check is
    # enforced in the dispatch wrapper (see 3B-5) for socket calls.
    # For MCP (Starlette) calls, we cannot extract agent_id here, so remote
    # is already blocked above and local MCP callers are trusted.

    engine = _require_task_engine()
    return engine.create_task(
        title=title,
        role=role,
        priority=priority,
        directory=directory,
        description=description,
        acceptance_criteria=acceptance_criteria,
        depends_on=depends_on,
        context_queries=context_queries,
        on_completion=on_completion,
    )
```

### task_claim

```
@mcp_server.tool()
def task_claim(task_id: str, host: str = "", model: str = "", pid: int = 0) -> dict:
    """Claim an open task for this agent. Starts the heartbeat clock.

    Args:
        task_id: The task to claim.
        host: Hostname of the claiming agent (auto-detected if empty).
        model: Model identifier of the claiming agent (optional).
        pid: Process ID of the claiming agent (optional).
    """
    # ── Block remote claim ──
    if transport_ctx.get().is_remote:
        return {"status": "error", "message": "Task claiming not permitted via remote transport"}

    engine = _require_task_engine()
    # agent_id is passed through the dispatch wrapper for socket calls.
    # For MCP calls, host/model/pid provide sufficient identity.
    return engine.claim_task(task_id=task_id, host=host, model=model, pid=pid)
```

### task_update

```
@mcp_server.tool()
def task_update(
    task_id: str,
    notes: str = "",
    phase: str = "",
    action: str = "",
    progress: int = -1,
    files: list[str] = [],
    blockers: list[str] = [],
    criteria_done: list[int] = [],
) -> dict:
    """Update progress on a claimed task. Only the claiming agent can update.

    Args:
        task_id: The task to update.
        notes: Free-text progress notes to append.
        phase: Current phase label (e.g. "pseudocode", "implement", "test").
        action: Current action label (e.g. "editing server.py").
        progress: Progress percentage (0-100). -1 means no change.
        files: List of files modified in this update.
        blockers: List of current blockers (replaces previous list).
        criteria_done: Indices of acceptance criteria now satisfied.
    """
    # ── Block remote update ──
    if transport_ctx.get().is_remote:
        return {"status": "error", "message": "Task updates not permitted via remote transport"}

    engine = _require_task_engine()
    # TaskEngine.update_task enforces claiming-agent ownership internally
    return engine.update_task(
        task_id=task_id,
        notes=notes,
        phase=phase,
        action=action,
        progress=progress,
        files=files,
        blockers=blockers,
        criteria_done=criteria_done,
    )
```

### task_complete

```
@mcp_server.tool()
def task_complete(task_id: str, result: str = "") -> dict:
    """Mark a claimed task as complete. Only the claiming agent can complete.

    Args:
        task_id: The task to complete.
        result: Summary of what was accomplished.
    """
    # ── Block remote completion ──
    if transport_ctx.get().is_remote:
        return {"status": "error", "message": "Task completion not permitted via remote transport"}

    engine = _require_task_engine()
    # TaskEngine.complete_task enforces claiming-agent ownership internally
    return engine.complete_task(task_id=task_id, result=result)
```

### task_release

```
@mcp_server.tool()
def task_release(task_id: str, reason: str = "") -> dict:
    """Release a claimed task back to open status. Only the claiming agent can release.

    Args:
        task_id: The task to release.
        reason: Why the task is being released (e.g. "blocked", "wrong role").
    """
    # ── Block remote release ──
    if transport_ctx.get().is_remote:
        return {"status": "error", "message": "Task release not permitted via remote transport"}

    engine = _require_task_engine()
    # TaskEngine.release_task enforces claiming-agent ownership internally
    return engine.release_task(task_id=task_id, reason=reason)
```


## 3B-5. build_task_dispatch (vaire/server.py)

Follows the `build_groomer_dispatch` pattern. Key difference from `build_dispatch_table`:
the task wrappers forward `agent_id` to the TaskEngine (the standard dispatch strips it).

```
def build_task_dispatch() -> dict:
    """Build task dispatch table — similar to build_groomer_dispatch.

    Each wrapper:
    - Accepts agent_id as first param (forwarded to TaskEngine for ownership checks)
    - task_create checks TASK_CREATE_ALLOWED prefix list
    - Read-only tools (task_list, task_get) run directly
    - Mutation tools run in executor to avoid blocking the event loop
    """
    if _task_engine is None:
        return {}

    engine = _task_engine
    _settings_local = settings  # capture module-level settings

    # ── Read-only wrappers (no executor needed) ─────────────────────

    async def task_list_handler(agent_id: str = "", **params):
        return engine.list_tasks(**params)

    async def task_get_handler(agent_id: str = "", **params):
        result = engine.get_task(**params)
        if result is None:
            return {"status": "error", "message": f"Task not found: {params.get('task_id', '?')}"}
        return result

    # ── Mutation wrappers (run in executor) ─────────────────────────

    async def task_create_handler(agent_id: str = "", **params):
        # Enforce TASK_CREATE_ALLOWED prefix list
        allowed_prefixes = _settings_local.task_create_allowed_list
        if allowed_prefixes:
            if not any(agent_id.startswith(prefix) for prefix in allowed_prefixes):
                return {
                    "status": "error",
                    "message": f"Agent '{agent_id}' not authorized to create tasks "
                               f"(allowed prefixes: {allowed_prefixes})",
                }

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: engine.create_task(**params))

    async def task_claim_handler(agent_id: str = "", **params):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: engine.claim_task(agent_id=agent_id, **params)
        )

    async def task_update_handler(agent_id: str = "", **params):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: engine.update_task(agent_id=agent_id, **params)
        )

    async def task_complete_handler(agent_id: str = "", **params):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: engine.complete_task(agent_id=agent_id, **params)
        )

    async def task_release_handler(agent_id: str = "", **params):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: engine.release_task(agent_id=agent_id, **params)
        )

    # ── Build dispatch dict ─────────────────────────────────────────
    # Names must match the MCP tool names exactly
    handlers = {
        "task_list": task_list_handler,
        "task_get": task_get_handler,
        "task_create": task_create_handler,
        "task_claim": task_claim_handler,
        "task_update": task_update_handler,
        "task_complete": task_complete_handler,
        "task_release": task_release_handler,
    }

    # Set __name__ for logging/debugging (same pattern as build_groomer_dispatch)
    for name, handler in handlers.items():
        handler.__name__ = name

    return handlers
```


## 3B-6. __main__.py wiring

In the `_run()` function, after `dispatch.update(build_ingest_dispatch(...))`:

```
from vaire.server import build_task_dispatch

# Add task tools to main dispatch table
task_dispatch = build_task_dispatch()
dispatch.update(task_dispatch)
if task_dispatch:
    logger.info("Task dispatch: %d tools registered", len(task_dispatch))
```

No changes to `VaireSocketServer` constructor — task tools go into the main
dispatch table (not a separate namespace like groomer_methods). This means:
- Rate limiting applies normally
- No special auth gate (unlike groomer which needs approved_groomers)
- Remote blocking is handled in the MCP tool functions themselves


## 3B-7. Permission model summary

### Remote transport blocking (in each @mcp_server.tool function)

Pattern: same as `forget`, `add_rule`, `ingest_file`, etc.

```
if transport_ctx.get().is_remote:
    return {"status": "error", "message": "... not permitted via remote transport"}
```

Applied to: `task_create`, `task_claim`, `task_update`, `task_complete`, `task_release`
NOT applied to: `task_list`, `task_get` (read-only, safe for remote)

### TASK_CREATE_ALLOWED prefix gating (in dispatch wrapper only)

The `task_create_handler` in `build_task_dispatch` checks `agent_id` against
`settings.task_create_allowed_list`. This only applies to socket-server calls
(which have agent_id). MCP Starlette calls go through the `@mcp_server.tool()`
path which has no agent_id — but remote is already blocked, and local MCP
callers are trusted.

If `TASK_CREATE_ALLOWED` is empty string, no prefix check is performed (all
local agents can create tasks).

### Claiming-agent ownership (enforced by TaskEngine)

`task_update`, `task_complete`, and `task_release` delegate ownership enforcement
to `TaskEngine` methods. The engine compares the calling agent's identity against
the task's `claimed_by` field. This is NOT duplicated in the MCP layer.

### Full permission matrix

| Tool | Local (MCP) | Local (socket) | Remote (mTLS) |
|---|---|---|---|
| task_list | Yes | Yes | Yes (read-only) |
| task_get | Yes | Yes | Yes (read-only) |
| task_create | Yes | TASK_CREATE_ALLOWED prefixes | Blocked |
| task_claim | Yes | Yes | Blocked |
| task_update | Yes (owner only*) | Yes (owner only*) | Blocked |
| task_complete | Yes (owner only*) | Yes (owner only*) | Blocked |
| task_release | Yes (owner only*) | Yes (owner only*) | Blocked |

*Owner enforcement by TaskEngine, not the MCP/dispatch layer.


## 3B-8. Export from server.py

Add `build_task_dispatch` to the module's public API (used by `__main__.py`):

```
# In the imports section of __main__.py, update:
from vaire.server import (
    async_shutdown,
    build_dispatch_table,
    build_groomer_dispatch,
    build_ingest_dispatch,
    build_task_dispatch,       # NEW
    init_engines,
    run_https,
    shutdown,
)
```


## Verification checklist

- [ ] `_task_engine` global declared alongside other globals
- [ ] `init_engines` creates TaskEngine in try/except (non-fatal)
- [ ] `_require_task_engine()` helper raises RuntimeError if None
- [ ] 7 MCP tools registered with `@mcp_server.tool()`
- [ ] 5 mutation tools check `transport_ctx.get().is_remote` and block
- [ ] 2 read tools (list, get) allow remote access
- [ ] `build_task_dispatch()` returns empty dict if `_task_engine is None`
- [ ] Dispatch wrappers forward `agent_id` to TaskEngine methods
- [ ] `task_create_handler` checks `TASK_CREATE_ALLOWED` prefix list
- [ ] `__main__.py` calls `build_task_dispatch()` and merges into dispatch
- [ ] Return conventions: dict for success, `{"status": "error", "message": ...}` for errors
- [ ] No `None` returns from MCP tools (task_get wraps None into error dict)
- [ ] Handler `__name__` attributes set for logging
- [ ] No shutdown hook needed (no background threads in Phase 3B)
