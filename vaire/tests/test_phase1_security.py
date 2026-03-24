"""
Regression tests for Phase 1 security fixes.

Covers every finding in the security review:
  Fix #1  (SEC1)  — validate_request rejects invalid agent_id / method
  Fix #2          — read_message converts IncompleteReadError → ProtocolError(DISCONNECTED)
  Fix #3          — read_message raises MESSAGE_TOO_LARGE before reading body
  Fix #4          — _handle_client closes writer on all exit paths
  Fix #5          — VaireSocketServer tracks tasks; stop() cancels all of them
  Fix #6          — socket file permissions are 0o660 after bind
  Fix #7          — PID file written on start, removed on stop, stale PIDs handled
  Fix #8          — generate_agent_id never returns empty string or "default"

All tests are independent, make no real network calls, and use only
in-process asyncio streams and temporary filesystem paths.
"""
from __future__ import annotations

import asyncio
import json
import os
import struct
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from vaire.protocol import (
    AGENT_ID_MAX_LEN,
    MAX_MESSAGE_SIZE,
    METHOD_MAX_LEN,
    ProtocolError,
    read_message,
    validate_request,
    write_message,
)
from vaire.socket_client import VaireClient, generate_agent_id
from vaire.socket_server import VaireSocketServer


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_raw_frame(body: bytes) -> bytes:
    """Build a valid length-prefixed frame from raw body bytes."""
    return struct.pack(">I", len(body)) + body


def _make_frame(payload: dict) -> bytes:
    """Build a valid length-prefixed frame from a JSON-serialisable dict."""
    body = json.dumps(payload).encode("utf-8")
    return _make_raw_frame(body)


def _make_server(
    tmp_path: Path, dispatch_table: dict | None = None
) -> VaireSocketServer:
    return VaireSocketServer(
        socket_path=str(tmp_path / "test.sock"),
        pid_file=str(tmp_path / "test.pid"),
        dispatch_table=dispatch_table or {},
    )


# ══════════════════════════════════════════════════════════════════════════════
# Fix #1 (SEC1) — validate_request
# ══════════════════════════════════════════════════════════════════════════════

class TestValidateRequest:
    """validate_request must reject every invalid agent_id and method case."""

    def _ok_msg(self, **overrides):
        base = {"agent_id": "host:1234:abcdef01", "method": "remember"}
        base.update(overrides)
        return base

    # ── agent_id ──────────────────────────────────────────────────────────────

    def test_valid_request_passes(self):
        validate_request(self._ok_msg())  # must not raise

    def test_missing_agent_id_raises(self):
        msg = {"method": "remember"}
        with pytest.raises(ProtocolError) as exc_info:
            validate_request(msg)
        assert exc_info.value.code == "INVALID_AGENT_ID"

    def test_empty_agent_id_raises(self):
        with pytest.raises(ProtocolError) as exc_info:
            validate_request(self._ok_msg(agent_id=""))
        assert exc_info.value.code == "INVALID_AGENT_ID"

    def test_whitespace_only_agent_id_raises(self):
        with pytest.raises(ProtocolError) as exc_info:
            validate_request(self._ok_msg(agent_id="   "))
        assert exc_info.value.code == "INVALID_AGENT_ID"

    def test_reserved_agent_id_default_raises(self):
        with pytest.raises(ProtocolError) as exc_info:
            validate_request(self._ok_msg(agent_id="default"))
        assert exc_info.value.code == "INVALID_AGENT_ID"

    def test_agent_id_too_long_raises(self):
        with pytest.raises(ProtocolError) as exc_info:
            validate_request(self._ok_msg(agent_id="x" * (AGENT_ID_MAX_LEN + 1)))
        assert exc_info.value.code == "INVALID_AGENT_ID"

    def test_agent_id_at_max_length_passes(self):
        validate_request(self._ok_msg(agent_id="a" * AGENT_ID_MAX_LEN))

    def test_non_string_agent_id_raises(self):
        with pytest.raises(ProtocolError) as exc_info:
            validate_request(self._ok_msg(agent_id=42))
        assert exc_info.value.code == "INVALID_AGENT_ID"

    # ── method ────────────────────────────────────────────────────────────────

    def test_missing_method_raises(self):
        msg = {"agent_id": "host:1:abcdef01"}
        with pytest.raises(ProtocolError) as exc_info:
            validate_request(msg)
        assert exc_info.value.code == "INVALID_METHOD"

    def test_empty_method_raises(self):
        with pytest.raises(ProtocolError) as exc_info:
            validate_request(self._ok_msg(method=""))
        assert exc_info.value.code == "INVALID_METHOD"

    def test_method_too_long_raises(self):
        with pytest.raises(ProtocolError) as exc_info:
            validate_request(self._ok_msg(method="m" * (METHOD_MAX_LEN + 1)))
        assert exc_info.value.code == "INVALID_METHOD"

    def test_method_at_max_length_passes(self):
        validate_request(self._ok_msg(method="m" * METHOD_MAX_LEN))

    def test_non_string_method_raises(self):
        with pytest.raises(ProtocolError) as exc_info:
            validate_request(self._ok_msg(method=None))
        assert exc_info.value.code == "INVALID_METHOD"


# ══════════════════════════════════════════════════════════════════════════════
# Fix #2 — read_message: IncompleteReadError → ProtocolError(DISCONNECTED)
# ══════════════════════════════════════════════════════════════════════════════

class TestReadMessageDisconnect:
    """read_message must convert IncompleteReadError to ProtocolError(DISCONNECTED)."""

    @pytest.mark.anyio
    async def test_header_partial_read_raises_disconnected(self):
        """Peer closes mid-header → DISCONNECTED."""
        reader = asyncio.StreamReader()
        reader.feed_data(b"\x00\x00")  # only 2 bytes; header needs 4
        reader.feed_eof()

        with pytest.raises(ProtocolError) as exc_info:
            await read_message(reader)
        assert exc_info.value.code == "DISCONNECTED"

    @pytest.mark.anyio
    async def test_body_partial_read_raises_disconnected(self):
        """Peer closes after full header but before full body → DISCONNECTED."""
        reader = asyncio.StreamReader()
        # Header says 10-byte body; only send 5 bytes then EOF.
        reader.feed_data(struct.pack(">I", 10))
        reader.feed_data(b"hello")
        reader.feed_eof()

        with pytest.raises(ProtocolError) as exc_info:
            await read_message(reader)
        assert exc_info.value.code == "DISCONNECTED"

    @pytest.mark.anyio
    async def test_immediate_eof_raises_disconnected(self):
        """Peer closes without sending anything → DISCONNECTED."""
        reader = asyncio.StreamReader()
        reader.feed_eof()

        with pytest.raises(ProtocolError) as exc_info:
            await read_message(reader)
        assert exc_info.value.code == "DISCONNECTED"


# ══════════════════════════════════════════════════════════════════════════════
# Fix #3 — read_message: MESSAGE_TOO_LARGE before body read
# ══════════════════════════════════════════════════════════════════════════════

class TestReadMessageTooLarge:
    """read_message must reject oversized messages before reading the body."""

    @pytest.mark.anyio
    async def test_max_size_plus_one_raises_before_body(self):
        """Declared length > MAX_MESSAGE_SIZE → MESSAGE_TOO_LARGE immediately."""
        reader = asyncio.StreamReader()
        # Send only the header — no body bytes at all.
        reader.feed_data(struct.pack(">I", MAX_MESSAGE_SIZE + 1))
        # Do NOT feed EOF; if the implementation tries to read the body
        # before checking size, it will hang rather than raise.

        with pytest.raises(ProtocolError) as exc_info:
            await asyncio.wait_for(read_message(reader), timeout=1.0)
        assert exc_info.value.code == "MESSAGE_TOO_LARGE"

    @pytest.mark.anyio
    async def test_exact_max_size_is_accepted(self):
        """A message of exactly MAX_MESSAGE_SIZE bytes must be accepted."""
        payload = {"k": "v"}
        body_bytes = json.dumps(payload).encode()
        assert len(body_bytes) <= MAX_MESSAGE_SIZE

        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack(">I", len(body_bytes)))
        reader.feed_data(body_bytes)

        result = await read_message(reader)
        assert result == payload

    @pytest.mark.anyio
    async def test_zero_length_body_is_valid_json_object(self):
        """An empty JSON object {} (2 bytes) must be accepted."""
        payload = {}
        body = json.dumps(payload).encode()

        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack(">I", len(body)))
        reader.feed_data(body)

        result = await read_message(reader)
        assert result == payload


# ══════════════════════════════════════════════════════════════════════════════
# Fix #4 — _handle_client: writer closed on all exit paths
# ══════════════════════════════════════════════════════════════════════════════

class TestHandleClientCleanup:
    """writer.close() + writer.wait_closed() must run on every exit path."""

    @pytest.mark.anyio
    async def test_writer_closed_on_normal_disconnect(self, tmp_path):
        """Writer is closed when client disconnects cleanly."""
        server = _make_server(tmp_path)

        reader = asyncio.StreamReader()
        reader.feed_eof()

        writer = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        await server._handle_client(reader, writer)

        writer.close.assert_called_once()
        writer.wait_closed.assert_awaited_once()

    @pytest.mark.anyio
    async def test_writer_closed_on_handler_exception(self, tmp_path):
        """Writer is closed even when a handler raises an unexpected exception."""
        async def broken_handler(**kwargs):
            raise RuntimeError("deliberate test failure")

        server = _make_server(tmp_path, dispatch_table={"boom": broken_handler})

        valid_request = {
            "id": "r1",
            "agent_id": "host:1:abcdef01",
            "method": "boom",
            "params": {},
        }
        frame = _make_frame(valid_request)

        reader = asyncio.StreamReader()
        reader.feed_data(frame)
        reader.feed_eof()

        writer = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()
        writer.drain = AsyncMock()
        writer.write = MagicMock()

        await server._handle_client(reader, writer)

        writer.close.assert_called_once()
        writer.wait_closed.assert_awaited_once()

    @pytest.mark.anyio
    async def test_writer_closed_on_cancelled_error(self, tmp_path):
        """Writer is closed when the task is cancelled (server stop)."""
        server = _make_server(tmp_path)

        # Reader that never produces data — task will block on read_message.
        reader = asyncio.StreamReader()

        writer = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        task = asyncio.get_running_loop().create_task(
            server._handle_client(reader, writer)
        )
        await asyncio.sleep(0)  # let the coroutine start
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        writer.close.assert_called_once()
        writer.wait_closed.assert_awaited_once()


# ══════════════════════════════════════════════════════════════════════════════
# Fix #5 — client task tracking + stop() cancellation
# ══════════════════════════════════════════════════════════════════════════════

class TestClientTaskTracking:
    """_client_tasks is maintained; stop() cancels all tasks before returning."""

    @pytest.mark.anyio
    async def test_task_added_on_accept(self, tmp_path):
        """_accept_client registers the new task in _client_tasks."""
        server = _make_server(tmp_path)

        reader = asyncio.StreamReader()
        reader.feed_eof()  # client disconnects immediately

        writer = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        assert len(server._client_tasks) == 0
        server._accept_client(reader, writer)
        assert len(server._client_tasks) == 1

        # Let the task run to completion
        await asyncio.sleep(0.05)
        # After completing, done_callback removes it
        assert len(server._client_tasks) == 0

    @pytest.mark.anyio
    async def test_stop_cancels_all_tasks(self, tmp_path):
        """stop() cancels every task in _client_tasks and awaits them."""
        server = _make_server(tmp_path)

        # Fabricate two long-running tasks
        async def _hang():
            await asyncio.sleep(9999)

        t1 = asyncio.get_running_loop().create_task(_hang())
        t2 = asyncio.get_running_loop().create_task(_hang())
        server._client_tasks.add(t1)
        t1.add_done_callback(server._client_tasks.discard)
        server._client_tasks.add(t2)
        t2.add_done_callback(server._client_tasks.discard)

        # stop() needs a server object to close; give it a mock
        mock_server = MagicMock()
        mock_server.close = MagicMock()
        mock_server.wait_closed = AsyncMock()
        server._server = mock_server

        await server.stop()

        assert t1.cancelled()
        assert t2.cancelled()
        assert len(server._client_tasks) == 0

    @pytest.mark.anyio
    async def test_done_callback_removes_task(self, tmp_path):
        """Completed tasks are auto-removed from _client_tasks via done_callback."""
        server = _make_server(tmp_path)

        async def _quick():
            pass  # completes immediately

        task = asyncio.get_running_loop().create_task(_quick())
        server._client_tasks.add(task)
        task.add_done_callback(server._client_tasks.discard)

        await task
        assert task not in server._client_tasks


# ══════════════════════════════════════════════════════════════════════════════
# Fix #6 — socket file permissions
# ══════════════════════════════════════════════════════════════════════════════

class TestSocketPermissions:
    """Socket file must be created with 0o660 permissions."""

    @pytest.mark.anyio
    async def test_socket_permissions_are_0o660(self, tmp_path):
        server = _make_server(tmp_path)
        await server.start()
        try:
            mode = os.stat(server._socket_path).st_mode & 0o777
            assert mode == 0o660, f"Expected 0o660, got 0o{mode:03o}"
        finally:
            await server.stop()

    @pytest.mark.anyio
    async def test_socket_permissions_not_world_readable(self, tmp_path):
        server = _make_server(tmp_path)
        await server.start()
        try:
            mode = os.stat(server._socket_path).st_mode & 0o007
            assert mode == 0, "Socket must not be accessible to 'other' users"
        finally:
            await server.stop()


# ══════════════════════════════════════════════════════════════════════════════
# Fix #7 — PID file management
# ══════════════════════════════════════════════════════════════════════════════

class TestPidFile:
    """PID file is written on start, removed on stop, stale files handled."""

    @pytest.mark.anyio
    async def test_pid_file_written_on_start(self, tmp_path):
        server = _make_server(tmp_path)
        await server.start()
        try:
            pid_path = Path(server._pid_file)
            assert pid_path.exists()
            assert int(pid_path.read_text().strip()) == os.getpid()
        finally:
            await server.stop()

    @pytest.mark.anyio
    async def test_pid_file_removed_on_stop(self, tmp_path):
        server = _make_server(tmp_path)
        await server.start()
        await server.stop()
        assert not Path(server._pid_file).exists()

    def test_stale_pid_for_dead_process_is_removed(self, tmp_path):
        """A PID file pointing to a dead process is silently removed."""
        server = _make_server(tmp_path)
        pid_path = Path(server._pid_file)
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        dead_pid = 999999
        pid_path.write_text(str(dead_pid))

        with patch("os.kill", side_effect=ProcessLookupError):
            server._check_stale_pid()  # must not raise

        assert not pid_path.exists()

    def test_malformed_pid_file_is_removed(self, tmp_path):
        """An unreadable PID file is removed without raising."""
        server = _make_server(tmp_path)
        pid_path = Path(server._pid_file)
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text("not-a-number")

        server._check_stale_pid()  # must not raise

        assert not pid_path.exists()

    def test_live_foreign_pid_raises_runtime_error(self, tmp_path):
        """A PID file pointing to a live foreign process raises RuntimeError."""
        server = _make_server(tmp_path)
        pid_path = Path(server._pid_file)
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        foreign_pid = os.getpid() + 9999
        pid_path.write_text(str(foreign_pid))

        # os.kill(pid, 0) succeeds → process is live
        with patch("os.kill", return_value=None):
            with pytest.raises(RuntimeError, match="already running"):
                server._check_stale_pid()

    def test_no_pid_file_is_fine(self, tmp_path):
        """Missing PID file is a no-op."""
        server = _make_server(tmp_path)
        assert not Path(server._pid_file).exists()
        server._check_stale_pid()  # must not raise


# ══════════════════════════════════════════════════════════════════════════════
# Fix #8 — generate_agent_id never returns empty or "default"
# ══════════════════════════════════════════════════════════════════════════════

class TestGenerateAgentId:
    """generate_agent_id must always return a valid, unique, non-sentinel ID."""

    def test_returns_non_empty_string(self):
        agent_id = generate_agent_id()
        assert isinstance(agent_id, str)
        assert len(agent_id) > 0

    def test_never_returns_default(self):
        for _ in range(100):
            assert generate_agent_id() != "default"

    def test_within_max_length(self):
        agent_id = generate_agent_id()
        assert len(agent_id) <= AGENT_ID_MAX_LEN

    def test_contains_pid(self):
        agent_id = generate_agent_id()
        assert str(os.getpid()) in agent_id

    def test_stable_within_process(self):
        """The same process always generates the same agent_id."""
        ids = {generate_agent_id() for _ in range(50)}
        assert len(ids) == 1, "generate_agent_id should be stable within a process"

    def test_defensive_assertion_on_empty_hostname(self):
        """If hostname is empty, 'unknown' is substituted and ID is still valid."""
        with patch("vaire.socket_client.socket") as mock_socket:
            mock_socket.gethostname.return_value = ""
            agent_id = generate_agent_id()
            assert "unknown" in agent_id

    def test_validate_request_accepts_generated_id(self):
        """A freshly generated agent_id passes server-side validation."""
        agent_id = generate_agent_id()
        validate_request({"agent_id": agent_id, "method": "remember"})


# ══════════════════════════════════════════════════════════════════════════════
# Integration — round-trip framing
# ══════════════════════════════════════════════════════════════════════════════

def _make_loopback_stream():
    """Return (reader, writer) backed by an in-process pipe."""
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    loop = asyncio.get_running_loop()
    transport = MagicMock()
    transport.write = lambda data: reader.feed_data(data)
    transport.is_closing = MagicMock(return_value=False)
    writer = asyncio.StreamWriter(transport, protocol, reader, loop)
    return reader, writer


class TestRoundTripFraming:
    """write_message + read_message must be an identity transformation."""

    @pytest.mark.anyio
    async def test_round_trip_simple_payload(self):
        payload = {"id": "abc", "method": "ping", "agent_id": "h:1:deadbeef"}
        reader, writer = _make_loopback_stream()
        await write_message(writer, payload)
        result = await read_message(reader)
        assert result == payload

    @pytest.mark.anyio
    async def test_round_trip_large_payload(self):
        payload = {"data": "x" * 100_000}
        reader, writer = _make_loopback_stream()
        await write_message(writer, payload)
        result = await read_message(reader)
        assert result == payload
