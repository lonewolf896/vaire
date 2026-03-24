"""
Vaire async Unix domain socket server.

One asyncio.Task per connected client. All SQLite writes are intended to go
through a write queue (Phase 5); this module owns the socket lifecycle,
connection dispatch, role enforcement, and PID/socket file management.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .crdt_sync import CRDTMemorySync

from .protocol import (
    ProtocolError,
    make_error_response,
    make_ok_response,
    read_message,
    validate_request,
    write_message,
)

logger = logging.getLogger(__name__)

# ── Groomer role ───────────────────────────────────────────────────────────────

# Methods that are only permitted for the groomer role (ZK-W3).
GROOMER_METHODS: frozenset[str] = frozenset({
    "groom_audit", "groom_inspect", "groom_duplicates", "groom_contradictions",
    "groom_orphans", "groom_stale", "groom_stats",
    "groom_merge", "groom_split", "groom_retag", "groom_reclassify",
    "groom_update_content", "groom_promote", "groom_demote", "groom_bulk_delete",
    "groom_auto",
})


# ── Connection state ───────────────────────────────────────────────────────────

@dataclass
class ConnectionState:
    """Per-connection metadata threaded through the read/dispatch loop."""

    agent_id: str = ""
    role: str = "agent"        # "agent" or "groomer"
    request_count: int = 0


# ── Server ─────────────────────────────────────────────────────────────────────

class VaireSocketServer:
    """Async Unix domain socket server for the Vaire shared memory service."""

    def __init__(
        self,
        socket_path: str,
        pid_file: str,
        dispatch_table: dict[str, Callable[..., Any]],
        groomer_id_prefix: str = "groomer-",
        max_clients: int = 32,
        crdt: CRDTMemorySync | None = None,
        groomer_methods: dict[str, Callable[..., Any]] | None = None,
    ) -> None:
        self._socket_path = socket_path
        self._pid_file = pid_file
        self._dispatch_table = dispatch_table
        self._groomer_id_prefix = groomer_id_prefix
        self._max_clients = max_clients
        self._crdt = crdt
        self._groomer_methods: dict[str, Callable[..., Any]] = groomer_methods or {}

        self._server: asyncio.Server | None = None
        # Fix #5 — every client task is tracked here; stop() cancels them all.
        self._client_tasks: set[asyncio.Task[None]] = set()

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Bind the socket, set permissions, write PID file, start accepting."""
        # Fix #7 — check for a stale/live PID file before binding.
        self._check_stale_pid()

        sock_path = Path(self._socket_path)
        sock_path.parent.mkdir(parents=True, exist_ok=True)
        if sock_path.exists():
            sock_path.unlink()

        self._server = await asyncio.start_unix_server(
            self._accept_client,
            path=self._socket_path,
        )

        # Fix #6 — restrict the socket to the owning user + group immediately
        # after bind, before any client can connect.
        os.chmod(self._socket_path, 0o660)

        # Fix #7 — record our PID so stop/status commands can find us.
        self._write_pid_file()

        logger.info("Vaire socket server listening on %s", self._socket_path)

    async def serve_forever(self) -> None:
        """Block until the server is stopped."""
        if self._server is None:
            raise RuntimeError("Call start() before serve_forever()")
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        """Cancel all active client tasks, close the server, clean up files."""
        # Fix #5 — cancel all tracked client tasks and wait for them to finish
        # before tearing down the rest of the server.
        if self._client_tasks:
            tasks = list(self._client_tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        # Fix #7 — remove PID file on clean stop.
        self._remove_pid_file()
        self._remove_socket_file()

        logger.info("Vaire socket server stopped")

    # ── Client acceptance ──────────────────────────────────────────────────────

    def _accept_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Callback invoked by asyncio for each new inbound connection."""
        if len(self._client_tasks) >= self._max_clients:
            logger.warning(
                "Max clients (%d) reached; rejecting connection",
                self._max_clients,
            )
            writer.close()
            return

        task: asyncio.Task[None] = asyncio.get_running_loop().create_task(
            self._handle_client(reader, writer)
        )
        # Fix #5 — register; done_callback auto-removes when the task finishes.
        self._client_tasks.add(task)
        task.add_done_callback(self._client_tasks.discard)

    # ── Per-client read/dispatch loop ──────────────────────────────────────────

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Read → validate → dispatch → respond loop for one connection.

        Fix #4 — the outer try/finally guarantees writer.close() +
        writer.wait_closed() run on EVERY exit path: normal return,
        CancelledError (from stop()), and unexpected exceptions.
        """
        state = ConnectionState()
        try:
            while True:
                # ── Read one message ───────────────────────────────────────
                try:
                    msg = await read_message(reader)
                except ProtocolError as exc:
                    if exc.code == "DISCONNECTED":
                        break
                    # Non-fatal framing error — inform the client and continue.
                    err = make_error_response(None, str(exc), exc.code)
                    try:
                        await write_message(writer, err)
                    except (OSError, ConnectionResetError):
                        break
                    continue

                request_id = msg.get("id")

                # ── SEC1: validate agent_id + method ───────────────────────
                try:
                    validate_request(msg)
                except ProtocolError as exc:
                    err = make_error_response(request_id, str(exc), exc.code)
                    try:
                        await write_message(writer, err)
                    except (OSError, ConnectionResetError):
                        break
                    continue

                # Latch agent identity on the first valid request.
                if not state.agent_id:
                    state.agent_id = msg["agent_id"]
                    state.role = self._resolve_role(state.agent_id)

                state.request_count += 1
                method = msg["method"]
                params = msg.get("params") or {}

                response = await self._dispatch_message(
                    request_id, method, params, state
                )
                try:
                    await write_message(writer, response)
                except (OSError, ConnectionResetError):
                    break

        except asyncio.CancelledError:
            # Server is shutting down — cleanup happens in finally, then propagate.
            raise
        except Exception:
            logger.exception(
                "Unexpected error in client handler for agent=%s",
                state.agent_id or "<unknown>",
            )
        finally:
            # Fix #4 — unconditional cleanup regardless of how we exited.
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass  # Connection may already be dead; nothing more we can do.

    # ── Dispatch ───────────────────────────────────────────────────────────────

    async def _dispatch_message(
        self,
        request_id: str | None,
        method: str,
        params: dict[str, Any],
        state: ConnectionState,
    ) -> dict[str, Any]:
        """Route *method* to the registered handler, enforcing role checks."""
        # ZK-W3: groomer-only methods are never exposed to regular agents.
        if method in GROOMER_METHODS and state.role != "groomer":
            return make_error_response(
                request_id,
                f"Method '{method}' requires groomer role",
                code="FORBIDDEN",
            )

        # Groomer agents can access both tables; groomer_methods takes precedence.
        if state.role == "groomer" and self._groomer_methods:
            table: dict[str, Any] = {**self._dispatch_table, **self._groomer_methods}
        else:
            table = self._dispatch_table

        handler = table.get(method)
        if handler is None:
            return make_error_response(
                request_id, f"Unknown method: {method}", code="NOT_FOUND"
            )

        if self._crdt is not None:
            self._crdt.set_active_agent(state.agent_id)

        # Strip agent_id from params to prevent duplicate keyword argument error.
        safe_params = {k: v for k, v in params.items() if k != "agent_id"}

        try:
            result = await handler(**safe_params, agent_id=state.agent_id)
            if not isinstance(result, dict):
                result = {"result": result}
            return make_ok_response(request_id, result)
        except Exception as exc:
            logger.exception("Handler %s raised: %s", method, exc)
            return make_error_response(
                request_id, str(exc), code="HANDLER_ERROR"
            )

    # ── Role resolution ────────────────────────────────────────────────────────

    def _resolve_role(self, agent_id: str) -> str:
        """Return 'groomer' if agent_id carries the groomer prefix, else 'agent'."""
        if agent_id.startswith(self._groomer_id_prefix):
            return "groomer"
        return "agent"

    # ── PID file helpers ───────────────────────────────────────────────────────

    def _check_stale_pid(self) -> None:
        """Handle a pre-existing PID file before binding.

        Fix #7 — three cases:
        - Unreadable / malformed file  → remove it and proceed.
        - PID belongs to a dead process → remove and proceed.
        - PID belongs to a live foreign process → raise RuntimeError.
        """
        pid_path = Path(self._pid_file)
        if not pid_path.exists():
            return

        try:
            raw = pid_path.read_text().strip()
            existing_pid = int(raw)
        except (OSError, ValueError):
            logger.warning(
                "PID file %s is unreadable or malformed; removing it",
                self._pid_file,
            )
            pid_path.unlink(missing_ok=True)
            return

        # Our own PID somehow survived (e.g. test re-use) — safe to overwrite.
        if existing_pid == os.getpid():
            return

        try:
            os.kill(existing_pid, 0)  # signal 0 = existence check only
        except ProcessLookupError:
            logger.info(
                "Removing stale PID file for dead process %d", existing_pid
            )
            pid_path.unlink(missing_ok=True)
            return
        except PermissionError:
            # Process exists but we cannot signal it — treat as live.
            raise RuntimeError(
                f"Vaire appears to be already running (PID {existing_pid}). "
                "Stop it first with 'vaire stop'."
            )

        raise RuntimeError(
            f"Vaire is already running (PID {existing_pid}). "
            "Stop it first with 'vaire stop'."
        )

    def _write_pid_file(self) -> None:
        pid_path = Path(self._pid_file)
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(os.getpid()))
        logger.debug(
            "PID file written: %s (pid=%d)", self._pid_file, os.getpid()
        )

    def _remove_pid_file(self) -> None:
        Path(self._pid_file).unlink(missing_ok=True)
        logger.debug("PID file removed: %s", self._pid_file)

    def _remove_socket_file(self) -> None:
        Path(self._socket_path).unlink(missing_ok=True)
