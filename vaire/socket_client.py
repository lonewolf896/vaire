"""
Vaire thin MCP client.

Proxies every MCP tool call to the shared Vaire socket server over a
Unix domain socket.  Has no state, no database connection, no embedding model.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import socket
from typing import Any

from mcp.server.fastmcp import FastMCP

from .protocol import ProtocolError, read_message, write_message

logger = logging.getLogger(__name__)

# ── Agent identity ─────────────────────────────────────────────────────────────

# One token per process lifetime — stays stable across reconnects.
# `secrets.token_hex` always returns a non-empty hex string, so this is
# never empty unless something deeply wrong happened with the runtime.
_SESSION_TOKEN: str = secrets.token_hex(16)


def generate_agent_id() -> str:
    """Return a unique, stable agent identifier for this client process.

    Format: ``{hostname}:{pid}:{token[:8]}``

    Raises:
        RuntimeError: if the generated ID is unexpectedly empty or equals
                      "default".  This should never happen in practice; the
                      check is a defensive assertion against future refactors.
    """
    hostname = socket.gethostname() or "unknown"
    pid = os.getpid()
    token_fragment = _SESSION_TOKEN[:8]
    agent_id = f"{hostname}:{pid}:{token_fragment}"

    # Defensive assertion — the format above cannot produce these values, but
    # we guard explicitly so a future refactor cannot silently regress.
    if not agent_id or agent_id == "default":
        raise RuntimeError(
            f"generate_agent_id produced an invalid identifier: {agent_id!r}"
        )

    return agent_id


# ── Client exception ───────────────────────────────────────────────────────────

class VaireError(Exception):
    """Raised when the server returns an error-status response."""

    def __init__(self, message: str, code: str = "UNKNOWN") -> None:
        super().__init__(message)
        self.code = code


# ── Timeout constant ───────────────────────────────────────────────────────────

# Default per-call timeout; overridden by VAIRE_CALL_TIMEOUT_SECONDS in config.
_DEFAULT_CALL_TIMEOUT: float = 30.0


# ── Client ─────────────────────────────────────────────────────────────────────

class VaireClient:
    """Async Unix socket client with multiplexed request–response matching.

    Maintains a background receiver task that reads all inbound messages and
    routes each one to the Future registered for its ``id`` by `call()`.
    """

    def __init__(
        self,
        socket_path: str,
        agent_id: str | None = None,
        call_timeout: float = _DEFAULT_CALL_TIMEOUT,
        auth_token: str | None = None,
    ) -> None:
        self._socket_path = socket_path
        self._agent_id = agent_id or generate_agent_id()
        self._call_timeout = call_timeout
        self._auth_token = auth_token

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        # Keyed by request id; each value is resolved by _recv_loop.
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._recv_task: asyncio.Task[None] | None = None
        # Lock to serialise concurrent auto-connect attempts.
        self._connect_lock: asyncio.Lock = asyncio.Lock()

    @property
    def agent_id(self) -> str:
        return self._agent_id

    async def connect(self, retries: int = 5, backoff: float = 0.5) -> None:
        """Open the Unix domain socket connection and start the receiver loop.

        Retries with exponential backoff when the socket file does not exist yet
        (e.g. the server container is still starting).
        """
        last_err: Exception | None = None
        for attempt in range(retries):
            try:
                self._reader, self._writer = await asyncio.open_unix_connection(
                    self._socket_path
                )
                self._recv_task = asyncio.get_running_loop().create_task(
                    self._recv_loop()
                )
                return
            except (FileNotFoundError, ConnectionRefusedError) as exc:
                last_err = exc
                if attempt < retries - 1:
                    await asyncio.sleep(backoff * (2 ** attempt))
        raise last_err  # type: ignore[misc]

    async def disconnect(self) -> None:
        """Gracefully close the connection and cancel the receiver loop."""
        if self._recv_task is not None:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
            self._recv_task = None

        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None

    async def _ensure_connected(self) -> None:
        """Connect (or reconnect) if the writer is absent or the recv task has exited."""
        needs_connect = self._writer is None or (
            self._recv_task is not None and self._recv_task.done()
        )
        if needs_connect:
            async with self._connect_lock:
                needs_connect = self._writer is None or (
                    self._recv_task is not None and self._recv_task.done()
                )
                if needs_connect:
                    # Clean up any stale state before reconnecting.
                    if self._writer is not None:
                        try:
                            self._writer.close()
                        except Exception:
                            pass
                        self._writer = None
                        self._reader = None
                    if self._recv_task is not None and not self._recv_task.done():
                        self._recv_task.cancel()
                    self._recv_task = None
                    await self.connect()

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send an RPC request and return the decoded result dict.

        Raises:
            VaireError:       if the server responds with status "error".
            asyncio.TimeoutError: if no response arrives within the timeout.
        """
        await self._ensure_connected()

        request_id = secrets.token_hex(8)
        payload: dict[str, Any] = {
            "id": request_id,
            "method": method,
            "agent_id": self._agent_id,
            "params": params,
        }
        if self._auth_token:
            payload["auth_token"] = self._auth_token

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future

        try:
            await write_message(self._writer, payload)
            response = await asyncio.wait_for(
                future, timeout=self._call_timeout
            )
        except OSError:
            # Broken pipe or connection reset — reset state and retry once.
            self._pending.pop(request_id, None)
            self._writer = None
            self._reader = None
            await self._ensure_connected()
            request_id = secrets.token_hex(8)
            payload["id"] = request_id
            future = loop.create_future()
            self._pending[request_id] = future
            try:
                await write_message(self._writer, payload)
                response = await asyncio.wait_for(
                    future, timeout=self._call_timeout
                )
            except (asyncio.TimeoutError, Exception):
                self._pending.pop(request_id, None)
                raise
        except (asyncio.TimeoutError, Exception):
            self._pending.pop(request_id, None)
            raise

        if response.get("status") == "error":
            raise VaireError(
                response.get("error", "Unknown error"),
                code=response.get("code", "UNKNOWN"),
            )

        return response.get("result", {})

    async def _recv_loop(self) -> None:
        """Background task: read responses and resolve matching futures."""
        assert self._reader is not None
        try:
            while True:
                try:
                    msg = await read_message(self._reader)
                except ProtocolError as exc:
                    if exc.code == "DISCONNECTED":
                        break
                    logger.warning("Protocol error from server: %s", exc)
                    continue

                response_id = msg.get("id")
                future = self._pending.pop(response_id, None)
                if future is not None and not future.done():
                    try:
                        future.set_result(msg)
                    except asyncio.InvalidStateError:
                        pass  # cancelled between done() check and set_result()
                else:
                    logger.debug(
                        "Received response for unknown/expired request id=%s",
                        response_id,
                    )

        except asyncio.CancelledError:
            pass
        finally:
            # Cancel all pending callers so they don't hang after disconnect.
            for fut in self._pending.values():
                if not fut.done():
                    fut.cancel()
            self._pending.clear()
            # Reset connection state so _ensure_connected() triggers on the next call.
            self._writer = None
            self._reader = None
            self._recv_task = None


# ── MCP thin proxy ─────────────────────────────────────────────────────────────

mcp = FastMCP(name="vaire")

# Module-level singleton; created lazily on first use so settings are resolved
# at runtime, not import time.
_client: VaireClient | None = None
_client_lock: asyncio.Lock | None = None


def _get_client_lock() -> asyncio.Lock:
    """Return (and lazily create) the module-level client creation lock."""
    global _client_lock
    if _client_lock is None:
        _client_lock = asyncio.Lock()
    return _client_lock


def _load_auth_token(settings) -> str | None:
    """Load the first available auth token from the tokens directory.

    Returns the token secret string, or None if auth is disabled or no
    token files exist.
    """
    if not settings.SOCKET_AUTH_ENABLED:
        return None

    tokens_dir = settings.socket_auth_tokens_dir_resolved
    if not tokens_dir.is_dir():
        return None

    # Look for a token file matching this host, then fall back to any token
    hostname = socket.gethostname() or "unknown"
    host_token = tokens_dir / f"{hostname}.token"
    if host_token.is_file():
        try:
            return host_token.read_text().strip()
        except OSError:
            pass

    # Fall back to first available token
    for path in sorted(tokens_dir.glob("*.token")):
        try:
            secret = path.read_text().strip()
            if secret:
                return secret
        except OSError:
            continue

    return None


def _build_client() -> VaireClient:
    from .config import get_settings

    settings = get_settings()
    auth_token = _load_auth_token(settings)
    return VaireClient(
        socket_path=str(settings.socket_path_resolved),
        agent_id=generate_agent_id(),
        call_timeout=float(settings.CALL_TIMEOUT_SECONDS),
        auth_token=auth_token,
    )


async def get_client_async() -> VaireClient:
    """Return the module-level client, creating it safely under an async lock."""
    global _client
    if _client is None:
        async with _get_client_lock():
            if _client is None:
                _client = _build_client()
    return _client


def get_client() -> VaireClient:
    """Return the module-level client, creating it on first call.

    Safe for single-threaded sync use.  Prefer get_client_async() when called
    from an async context to avoid a race between concurrent coroutines.
    """
    global _client
    if _client is None:
        _client = _build_client()
    return _client


# ── Async call helpers ─────────────────────────────────────────────────────────
# All @mcp.tool() stubs are async and may run concurrently; use the async
# client accessor so the singleton is created under a lock.

async def _client_call(method: str, params: dict) -> Any:
    """Fetch the client singleton (async-safe) and invoke method."""
    return await (await get_client_async()).call(method, params)


# ── MCP tool stubs ─────────────────────────────────────────────────────────────
# Each tool forwards its arguments verbatim to the server; no logic lives here.

@mcp.tool()
async def remember(
    content: str,
    context: str,
    tags: list[str] | None = None,
    force: bool = False,
) -> dict:
    """Store a memory in Vaire.

    force: bypass the write gate and store regardless of surprisal score.
    """
    params: dict = {"content": content, "context": context, "tags": tags or []}
    if force:
        params["force"] = True
    return await _client_call("remember", params)


@mcp.tool()
async def recall(
    query: str,
    context: str | None = None,
    max_results: int = 10,
    min_heat: float = 0.1,
    max_tokens: int | None = None,
    compact: bool = False,
    fast: bool = True,
) -> dict:
    """Retrieve memories matching a query.

    fast: if True (default), skip cross-encoder for ~130ms response.
          if False, run full deep reranking for highest quality (~6s).
    """
    params: dict = {
        "query": query, "context": context,
        "max_results": max_results, "min_heat": min_heat,
        "fast": fast,
    }
    if max_tokens is not None:
        params["max_tokens"] = max_tokens
    if compact:
        params["compact"] = True
    return await _client_call("recall", params)


@mcp.tool()
async def forget(memory_id: int) -> dict:
    """Delete a memory by ID."""
    return await _client_call("forget", {"memory_id": memory_id})


@mcp.tool()
async def get_project_context(directory: str, max_tokens: int | None = None, compact: bool = False) -> dict:
    """Return all hot memories for a directory."""
    params: dict = {"directory": directory}
    if max_tokens is not None:
        params["max_tokens"] = max_tokens
    if compact:
        params["compact"] = True
    return await _client_call("get_project_context", params)


@mcp.tool()
async def memory_stats() -> dict:
    """Return system memory statistics."""
    return await _client_call("memory_stats", {})


@mcp.tool()
async def consolidate_now() -> dict:
    """Trigger an immediate consolidation cycle."""
    return await _client_call("consolidate_now", {})


@mcp.tool()
async def rate_memory(memory_id: int, rating: float = 1.0, was_useful: bool | None = None) -> dict:
    """Rate a memory's usefulness."""
    params: dict = {"memory_id": memory_id, "rating": rating}
    if was_useful is not None:
        params["was_useful"] = was_useful
    return await _client_call("rate_memory", params)


@mcp.tool()
async def validate_memory(memory_id: int) -> dict:
    """Check a memory's validity against current file state."""
    return await _client_call("validate_memory", {"memory_id": memory_id})


@mcp.tool()
async def recall_hierarchical(
    query: str,
    level: int | None = None,
    max_results: int = 10,
    max_tokens: int | None = None,
    compact: bool = False,
) -> list:
    """Retrieve memories from the fractal hierarchy."""
    params: dict = {"query": query, "level": level, "max_results": max_results}
    if max_tokens is not None:
        params["max_tokens"] = max_tokens
    if compact:
        params["compact"] = True
    return await _client_call("recall_hierarchical", params)


@mcp.tool()
async def drill_down(cluster_id: int) -> list:
    """Drill into a cluster to see its members."""
    return await _client_call("drill_down", {"cluster_id": cluster_id})


@mcp.tool()
async def create_trigger(
    content: str,
    trigger_condition: str,
    trigger_type: str,
    target_directory: str | None = None,
) -> dict:
    """Create a prospective memory trigger."""
    return await _client_call(
        "create_trigger",
        {
            "content": content,
            "trigger_condition": trigger_condition,
            "trigger_type": trigger_type,
            "target_directory": target_directory,
        },
    )


@mcp.tool()
async def get_project_story(directory: str) -> str:
    """Get the autobiographical narrative for a project directory."""
    return await _client_call("get_project_story", {"directory": directory})


@mcp.tool()
async def add_rule(
    rule_type: str,
    scope: str,
    condition: str,
    action: str,
    priority: int = 0,
    scope_value: str = "",
) -> dict:
    """Add a neuro-symbolic rule for filtering/re-ranking memories.

    Args:
        rule_type: "hard" (must satisfy — uses "filter" action only) or "soft" (preference — uses boost/penalty).
        scope: "global" (all memories), "directory" (match scope_value path), or "file" (match scope_value pattern).
        condition: Format is "field operator value". Operators: ==, !=, >, <, >=, <=, contains, not_contains, matches.
            Fields: tag, content, directory_context, importance, heat, confidence, surprise_score,
            emotional_valence, plasticity, stability, excitability, access_count, useful_count.
            Examples: "importance > 0.7", "tag contains architecture", "directory_context matches /project/*".
        action: "filter" (hard rules only — excludes non-matching memories),
            "boost:N" (soft rules — multiply score by N, e.g. "boost:1.5"),
            or "penalty:N" (soft rules — reduce score by N, e.g. "penalty:0.3").
        priority: Higher values are applied first. Default 0.
        scope_value: Directory path or file glob pattern. Required when scope is "directory" or "file".
    """
    return await _client_call(
        "add_rule",
        {
            "rule_type": rule_type,
            "scope": scope,
            "condition": condition,
            "action": action,
            "priority": priority,
            "scope_value": scope_value,
        },
    )


@mcp.tool()
async def get_rules(directory: str = "") -> list:
    """Get active rules."""
    return await _client_call("get_rules", {"directory": directory})


@mcp.tool()
async def navigate_memory(query: str, top_k: int = 5) -> list:
    """Navigate concept space using Successor Representation cognitive maps."""
    return await _client_call("navigate_memory", {"query": query, "top_k": top_k})


@mcp.tool()
async def get_causal_chain(entity: str) -> dict:
    """Get causal causes and effects for an entity."""
    return await _client_call("get_causal_chain", {"entity": entity})


@mcp.tool()
async def assess_coverage(query: str, directory: str = "") -> dict:
    """Assess how well Vaire knows about a topic."""
    return await _client_call(
        "assess_coverage", {"query": query, "directory": directory}
    )


@mcp.tool()
async def detect_gaps(directory: str) -> list:
    """Detect knowledge gaps for a project directory."""
    return await _client_call("detect_gaps", {"directory": directory})


@mcp.tool()
async def checkpoint(
    directory: str,
    current_task: str = "",
    files_being_edited: list[str] | None = None,
    key_decisions: list[str] | None = None,
    open_questions: list[str] | None = None,
    next_steps: list[str] | None = None,
    active_errors: list[str] | None = None,
    custom_context: str = "",
) -> dict:
    """Snapshot your current working state for post-compaction recovery."""
    return await _client_call(
        "checkpoint",
        {
            "directory": directory,
            "current_task": current_task,
            "files_being_edited": files_being_edited,
            "key_decisions": key_decisions,
            "open_questions": open_questions,
            "next_steps": next_steps,
            "active_errors": active_errors,
            "custom_context": custom_context,
        },
    )


@mcp.tool()
async def restore(directory: str = "") -> dict:
    """Restore context after compaction using Hippocampal Replay."""
    return await _client_call("restore", {"directory": directory})


@mcp.tool()
async def anchor(content: str, context: str, reason: str = "") -> dict:
    """Mark critical context as compaction-resistant."""
    return await _client_call(
        "anchor", {"content": content, "context": context, "reason": reason}
    )


@mcp.tool()
async def install_hooks(project_directory: str = "") -> dict:
    """Install Claude Code hooks for automatic memory capture and replay."""
    return await _client_call(
        "install_hooks", {"project_directory": project_directory}
    )


@mcp.tool()
async def sync_instructions(claude_md_path: str = "") -> dict:
    """Sync Vaire instructions into the global CLAUDE.md file."""
    return await _client_call(
        "sync_instructions", {"claude_md_path": claude_md_path}
    )


@mcp.tool()
async def ingest_file(file_path: str, dry_run: bool = False) -> dict:
    """Ingest a markdown/text file into Vaire's memory store."""
    return await _client_call(
        "ingest_file", {"file_path": file_path, "dry_run": dry_run}
    )


@mcp.tool()
async def ingest_directory(directory_path: str, recursive: bool = True) -> dict:
    """Ingest all supported files in a directory into Vaire."""
    return await _client_call(
        "ingest_directory",
        {"directory_path": directory_path, "recursive": recursive},
    )


@mcp.tool()
async def ingest_status(job_id: str) -> dict:
    """Get the status of a running or completed ingestion job."""
    return await _client_call("ingest_status", {"job_id": job_id})


@mcp.tool()
async def ingest_preview(file_path: str) -> dict:
    """Preview how a file would be chunked without writing to storage."""
    return await _client_call("ingest_preview", {"file_path": file_path})


# ── Groomer MCP instance ───────────────────────────────────────────────────────

groomer_mcp = FastMCP(name="vaire-groomer")

# Module-level singleton for the groomer client; uses a groomer- prefixed agent_id.
_groomer_client: VaireClient | None = None
_groomer_client_lock: asyncio.Lock | None = None


def _get_groomer_client_lock() -> asyncio.Lock:
    global _groomer_client_lock
    if _groomer_client_lock is None:
        _groomer_client_lock = asyncio.Lock()
    return _groomer_client_lock


def _build_groomer_client() -> VaireClient:
    import configparser
    from pathlib import Path
    from .config import get_settings
    settings = get_settings()
    # Read the approved groomer agent_id from ~/.vaire/vaire.ini
    ini_path = Path(settings.DB_PATH).expanduser().parent / "vaire.ini"
    groomer_agent_id = ""
    if ini_path.exists():
        cfg = configparser.ConfigParser()
        cfg.read(ini_path)
        raw = cfg.get("groomer", "approved", fallback="")
        ids = [g.strip() for g in raw.split(",") if g.strip()]
        if ids:
            groomer_agent_id = ids[0]
    if not groomer_agent_id:
        groomer_agent_id = f"groomer-{_SESSION_TOKEN[:8]}"

    # Load auth token — prefer a token matching the groomer agent_id
    auth_token = None
    if settings.SOCKET_AUTH_ENABLED:
        tokens_dir = settings.socket_auth_tokens_dir_resolved
        groomer_token_path = tokens_dir / f"{groomer_agent_id}.token"
        if groomer_token_path.is_file():
            try:
                auth_token = groomer_token_path.read_text().strip()
            except OSError:
                pass
        if not auth_token:
            auth_token = _load_auth_token(settings)

    return VaireClient(
        socket_path=str(settings.socket_path_resolved),
        agent_id=groomer_agent_id,
        call_timeout=float(settings.CALL_TIMEOUT_SECONDS),
        auth_token=auth_token,
    )


async def get_groomer_client_async() -> VaireClient:
    """Return the groomer client singleton, creating it safely under an async lock."""
    global _groomer_client
    if _groomer_client is None:
        async with _get_groomer_client_lock():
            if _groomer_client is None:
                _groomer_client = _build_groomer_client()
    return _groomer_client


def get_groomer_client() -> VaireClient:
    """Return the groomer client singleton, creating it on first call.

    Prefer get_groomer_client_async() from async contexts.
    """
    global _groomer_client
    if _groomer_client is None:
        _groomer_client = _build_groomer_client()
    return _groomer_client


# ── Groomer async call helper ──────────────────────────────────────────────────

async def _groomer_call(method: str, params: dict) -> Any:
    """Fetch the groomer client singleton (async-safe) and invoke method."""
    return await (await get_groomer_client_async()).call(method, params)


# ── Groomer tool stubs ─────────────────────────────────────────────────────────


@groomer_mcp.tool()
async def groom_audit(
    directory: str | None = None,
    min_age_days: int | None = None,
    max_heat: float | None = None,
    tags: list[str] | None = None,
    store_type: str | None = None,
    provenance_agent: str | None = None,
    content_length_min: int | None = None,
    content_length_max: int | None = None,
    tags_empty: bool | None = None,
    id_range: list[int] | None = None,
    limit: int = 50,
) -> list:
    """Browse the corpus with optional filters, ordered oldest/coldest first.

    Extended filters: provenance_agent, content_length_min/max, tags_empty (bool), id_range ([min, max]).
    """
    return await _groomer_call(
        "groom_audit",
        {
            "directory": directory,
            "min_age_days": min_age_days,
            "max_heat": max_heat,
            "tags": tags,
            "store_type": store_type,
            "provenance_agent": provenance_agent,
            "content_length_min": content_length_min,
            "content_length_max": content_length_max,
            "tags_empty": tags_empty,
            "id_range": id_range,
            "limit": limit,
        },
    )


@groomer_mcp.tool()
async def groom_inspect(memory_id: int) -> dict:
    """Return the full memory record including archive history."""
    return await _groomer_call("groom_inspect", {"memory_id": memory_id})


@groomer_mcp.tool()
async def groom_duplicates(
    similarity_threshold: float = 0.85,
    directory: str | None = None,
    limit: int = 50,
) -> list:
    """Return candidate duplicate groups."""
    return await _groomer_call(
        "groom_duplicates",
        {
            "similarity_threshold": similarity_threshold,
            "directory": directory,
            "limit": limit,
        },
    )


@groomer_mcp.tool()
async def groom_contradictions(
    directory: str | None = None,
    limit: int = 50,
) -> list:
    """Scan for memories with negation mismatches or action divergence."""
    return await _groomer_call(
        "groom_contradictions", {"directory": directory, "limit": limit}
    )


@groomer_mcp.tool()
async def groom_orphans(
    directory: str | None = None,
    max_heat: float = 0.15,
    include_tagged: bool = False,
    min_content_length: int | None = None,
    limit: int = 50,
) -> list:
    """Return low-connectivity memories. Configurable thresholds.

    Args:
        max_heat: Heat threshold (default 0.15).
        include_tagged: If True, include tagged memories too (default False = untagged only).
        min_content_length: Minimum content length to include (catches keyword stubs).
    """
    return await _groomer_call(
        "groom_orphans",
        {
            "directory": directory,
            "max_heat": max_heat,
            "include_tagged": include_tagged,
            "min_content_length": min_content_length,
            "limit": limit,
        },
    )


@groomer_mcp.tool()
async def groom_stale(directory: str | None = None, limit: int = 50) -> list:
    """Return memories flagged as stale."""
    return await _groomer_call(
        "groom_stale", {"directory": directory, "limit": limit}
    )


@groomer_mcp.tool()
async def groom_stats(directory: str | None = None) -> dict:
    """Return grooming-specific corpus statistics."""
    return await _groomer_call("groom_stats", {"directory": directory})


@groomer_mcp.tool()
async def groom_merge(
    memory_ids: list[int],
    merged_content: str,
    merged_tags: list[str] | None = None,
) -> dict:
    """Merge N memories into one; archive the originals."""
    return await _groomer_call(
        "groom_merge",
        {
            "memory_ids": memory_ids,
            "merged_content": merged_content,
            "merged_tags": merged_tags or [],
        },
    )


@groomer_mcp.tool()
async def groom_split(memory_id: int, splits: list[dict]) -> dict:
    """Split one memory into N; archive the original."""
    return await _groomer_call(
        "groom_split", {"memory_id": memory_id, "splits": splits}
    )


@groomer_mcp.tool()
async def groom_retag(memory_id: int, new_tags: list[str]) -> dict:
    """Replace a memory's tags."""
    return await _groomer_call(
        "groom_retag", {"memory_id": memory_id, "new_tags": new_tags}
    )


@groomer_mcp.tool()
async def groom_reclassify(memory_id: int, new_directory: str) -> dict:
    """Move a memory to a different directory context."""
    return await _groomer_call(
        "groom_reclassify",
        {"memory_id": memory_id, "new_directory": new_directory},
    )


@groomer_mcp.tool()
async def groom_update_content(memory_id: int, new_content: str) -> dict:
    """Rewrite a memory's content; re-embeds automatically."""
    return await _groomer_call(
        "groom_update_content",
        {"memory_id": memory_id, "new_content": new_content},
    )


@groomer_mcp.tool()
async def groom_promote(memory_id: int) -> dict:
    """Boost a memory: heat=1.0, protected, _anchor tag."""
    return await _groomer_call("groom_promote", {"memory_id": memory_id})


@groomer_mcp.tool()
async def groom_demote(memory_id: int) -> dict:
    """Demote a memory: heat=0.01, unprotected."""
    return await _groomer_call("groom_demote", {"memory_id": memory_id})


@groomer_mcp.tool()
async def groom_bulk_delete(filter: dict) -> dict:
    """Delete memories matching filter dict (at least one criterion required)."""
    return await _groomer_call("groom_bulk_delete", {"filter": filter})


@groomer_mcp.tool()
async def groom_auto(directory: str | None = None, depth: str = "light") -> dict:
    """Run a structured grooming pass (depth: light/medium/deep)."""
    return await _groomer_call(
        "groom_auto", {"directory": directory, "depth": depth}
    )


@groomer_mcp.tool()
async def groom_forget(memory_ids: list[int], reason: str = "groomer") -> dict:
    """Delete memories by ID list. Archives each to grooming_archives first."""
    return await _groomer_call(
        "groom_forget", {"memory_ids": memory_ids, "reason": reason}
    )


@groomer_mcp.tool()
async def groom_search(
    provenance_agent: str | None = None,
    content_like: str | None = None,
    content_length_min: int | None = None,
    content_length_max: int | None = None,
    tags_empty: bool | None = None,
    created_before: str | None = None,
    created_after: str | None = None,
    id_range: list[int] | None = None,
    directory: str | None = None,
    limit: int = 50,
    fields: list[str] | None = None,
) -> list:
    """Raw filtered query — no embedding search, no reranking. Just a filtered SELECT.

    Args:
        provenance_agent: Filter by agent that created the memory.
        content_like: SQL LIKE pattern match on content (wraps in %...%).
        content_length_min: Minimum content length in characters.
        content_length_max: Maximum content length in characters.
        tags_empty: True = only untagged memories, False = only tagged.
        created_before: ISO datetime upper bound.
        created_after: ISO datetime lower bound.
        id_range: [min_id, max_id] inclusive range.
        directory: Filter by directory_context.
        limit: Max results (default 50).
        fields: Return only these fields (id always included).
    """
    return await _groomer_call(
        "groom_search",
        {
            "provenance_agent": provenance_agent,
            "content_like": content_like,
            "content_length_min": content_length_min,
            "content_length_max": content_length_max,
            "tags_empty": tags_empty,
            "created_before": created_before,
            "created_after": created_after,
            "id_range": id_range,
            "directory": directory,
            "limit": limit,
            "fields": fields,
        },
    )


@groomer_mcp.tool()
async def groom_bulk_retag(
    filter: dict,
    new_tags: list[str],
    mode: str = "replace",
) -> dict:
    """Retag multiple memories matching a filter.

    Args:
        filter: Same keys as groom_audit (directory, min_age_days, max_heat, tags, store_type, limit).
        new_tags: Tags to apply.
        mode: "replace" (overwrite), "append" (add without removing), or "remove" (remove these tags).
    """
    return await _groomer_call(
        "groom_bulk_retag",
        {"filter": filter, "new_tags": new_tags, "mode": mode},
    )


@groomer_mcp.tool()
async def groom_content_scan(
    pattern: str,
    replacement: str | None = None,
    dry_run: bool = True,
) -> dict:
    """Scan all memories for a regex pattern. Optionally find-and-replace (re-embeds automatically).

    Args:
        pattern: Python regex pattern to search for.
        replacement: If given with dry_run=False, replaces matches and re-embeds affected memories.
        dry_run: If True (default), only reports matches without modifying anything.
    """
    return await _groomer_call(
        "groom_content_scan",
        {"pattern": pattern, "replacement": replacement, "dry_run": dry_run},
    )


@groomer_mcp.tool()
async def groom_bulk_update_content(
    memory_ids: list[int],
    find: str,
    replace: str,
) -> dict:
    """Find/replace across specified memory IDs. Re-embeds all affected memories."""
    return await _groomer_call(
        "groom_bulk_update_content",
        {"memory_ids": memory_ids, "find": find, "replace": replace},
    )


@groomer_mcp.tool()
async def groom_sanitize_archives(dry_run: bool = True) -> dict:
    """Scan all archive entries for credential patterns and redact them.

    Args:
        dry_run: If True (default), only reports matches without modifying anything.
    """
    return await _groomer_call(
        "groom_sanitize_archives", {"dry_run": dry_run},
    )


@groomer_mcp.tool()
async def groom_provenance(directory: str | None = None) -> list:
    """List distinct provenance_agent values with counts, date ranges, and top tags."""
    return await _groomer_call(
        "groom_provenance", {"directory": directory}
    )
