"""
Tests for TASK-022: Unix Socket Authentication.

Covers:
  - TokenManager CRUD: create, validate, revoke, list, has_tokens
  - Token validation rejects invalid/missing tokens
  - Agent name validation (path separators, dots, empty, length)
  - VaireSocketServer authentication integration:
      - Authenticated connections are accepted
      - Unauthenticated connections are rejected when tokens exist
      - Auth disabled allows unauthenticated connections
      - Migration grace period: no tokens exist → allow unauthenticated
      - Server-derived agent_id overrides self-reported identity
  - VaireClient includes auth_token in payloads
  - Config settings: SOCKET_AUTH_ENABLED, SOCKET_AUTH_TOKENS_DIR
"""
from __future__ import annotations

import asyncio
import json
import os
import struct
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from vaire.token_manager import TokenManager, TokenInfo
from vaire.socket_server import ConnectionState, VaireSocketServer
from vaire.protocol import make_ok_response, read_message, write_message


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_frame(payload: dict) -> bytes:
    """Build a valid length-prefixed frame from a JSON-serialisable dict."""
    body = json.dumps(payload).encode("utf-8")
    return struct.pack(">I", len(body)) + body


async def _read_response(reader: asyncio.StreamReader) -> dict:
    """Read one length-prefixed JSON response."""
    header = await reader.readexactly(4)
    (length,) = struct.unpack(">I", header)
    body = await reader.readexactly(length)
    return json.loads(body.decode("utf-8"))


# ── TokenManager unit tests ──────────────────────────────────────────────────


class TestTokenManager:
    """Unit tests for TokenManager CRUD operations."""

    def test_create_and_validate(self, tmp_path):
        mgr = TokenManager(tmp_path / "tokens")
        secret = mgr.create("test-agent")

        assert secret
        assert len(secret) == 64  # 32 bytes hex
        assert mgr.validate(secret) == "test-agent"

    def test_validate_wrong_token(self, tmp_path):
        mgr = TokenManager(tmp_path / "tokens")
        mgr.create("test-agent")

        assert mgr.validate("wrong-token-value") is None

    def test_validate_empty_token(self, tmp_path):
        mgr = TokenManager(tmp_path / "tokens")
        mgr.create("test-agent")

        assert mgr.validate("") is None
        assert mgr.validate(None) is None

    def test_revoke(self, tmp_path):
        mgr = TokenManager(tmp_path / "tokens")
        secret = mgr.create("test-agent")

        assert mgr.revoke("test-agent") is True
        assert mgr.validate(secret) is None

    def test_revoke_nonexistent(self, tmp_path):
        mgr = TokenManager(tmp_path / "tokens")
        assert mgr.revoke("no-such-agent") is False

    def test_list_tokens(self, tmp_path):
        mgr = TokenManager(tmp_path / "tokens")
        mgr.create("alice")
        mgr.create("bob")

        tokens = mgr.list_tokens()
        names = {t.agent_name for t in tokens}
        assert names == {"alice", "bob"}
        assert all(isinstance(t, TokenInfo) for t in tokens)
        assert all(t.created_at > 0 for t in tokens)

    def test_list_tokens_empty(self, tmp_path):
        mgr = TokenManager(tmp_path / "tokens")
        assert mgr.list_tokens() == []

    def test_has_tokens(self, tmp_path):
        mgr = TokenManager(tmp_path / "tokens")
        assert mgr.has_tokens() is False

        mgr.create("agent-1")
        assert mgr.has_tokens() is True

        mgr.revoke("agent-1")
        assert mgr.has_tokens() is False

    def test_create_overwrites_existing(self, tmp_path):
        mgr = TokenManager(tmp_path / "tokens")
        secret1 = mgr.create("test-agent")
        secret2 = mgr.create("test-agent")

        assert secret1 != secret2
        assert mgr.validate(secret1) is None
        assert mgr.validate(secret2) == "test-agent"

    def test_token_file_permissions(self, tmp_path):
        mgr = TokenManager(tmp_path / "tokens")
        mgr.create("secure-agent")

        token_path = mgr.tokens_dir / "secure-agent.token"
        mode = oct(token_path.stat().st_mode & 0o777)
        assert mode == "0o600"

    def test_tokens_dir_permissions(self, tmp_path):
        mgr = TokenManager(tmp_path / "tokens")
        mgr.create("test-agent")

        mode = oct(mgr.tokens_dir.stat().st_mode & 0o777)
        assert mode == "0o700"

    def test_multiple_agents(self, tmp_path):
        mgr = TokenManager(tmp_path / "tokens")
        s1 = mgr.create("agent-a")
        s2 = mgr.create("agent-b")
        s3 = mgr.create("agent-c")

        assert mgr.validate(s1) == "agent-a"
        assert mgr.validate(s2) == "agent-b"
        assert mgr.validate(s3) == "agent-c"


class TestAgentNameValidation:
    """Test that invalid agent names are rejected."""

    def test_empty_name(self, tmp_path):
        mgr = TokenManager(tmp_path / "tokens")
        with pytest.raises(ValueError, match="non-empty"):
            mgr.create("")

    def test_path_separator_slash(self, tmp_path):
        mgr = TokenManager(tmp_path / "tokens")
        with pytest.raises(ValueError, match="path separators"):
            mgr.create("../../etc/passwd")

    def test_path_separator_backslash(self, tmp_path):
        mgr = TokenManager(tmp_path / "tokens")
        with pytest.raises(ValueError, match="path separators"):
            mgr.create("foo\\bar")

    def test_dot_prefix(self, tmp_path):
        mgr = TokenManager(tmp_path / "tokens")
        with pytest.raises(ValueError, match="start with"):
            mgr.create(".hidden")

    def test_too_long(self, tmp_path):
        mgr = TokenManager(tmp_path / "tokens")
        with pytest.raises(ValueError, match="too long"):
            mgr.create("a" * 200)

    def test_null_byte(self, tmp_path):
        mgr = TokenManager(tmp_path / "tokens")
        with pytest.raises(ValueError, match="path separators"):
            mgr.create("agent\x00name")


# ── VaireSocketServer authentication integration tests ────────────────────────


@pytest.fixture
def token_env(tmp_path):
    """Set up a token manager with one token, return (manager, secret, agent_name)."""
    tokens_dir = tmp_path / "tokens"
    mgr = TokenManager(tokens_dir)
    secret = mgr.create("claude-agent")
    return mgr, secret, "claude-agent"


def _make_dispatch() -> dict:
    """Return a simple dispatch table with a memory_stats handler."""
    async def _handler(**kwargs):
        return {"total": 42, "agent_id_seen": kwargs.get("agent_id", "")}
    return {"memory_stats": _handler}


@pytest.mark.asyncio
async def test_auth_success(tmp_path, token_env):
    """Authenticated request succeeds and returns server-derived agent_id."""
    mgr, secret, agent_name = token_env
    sock_path = str(tmp_path / "test.sock")
    pid_path = str(tmp_path / "test.pid")

    server = VaireSocketServer(
        socket_path=sock_path,
        pid_file=pid_path,
        dispatch_table=_make_dispatch(),
        token_manager=mgr,
        auth_enabled=True,
    )
    await server.start()

    try:
        reader, writer = await asyncio.open_unix_connection(sock_path)

        # Send authenticated request
        payload = {
            "id": "req-1",
            "method": "memory_stats",
            "agent_id": "self-reported-ignored",
            "auth_token": secret,
            "params": {},
        }
        await write_message(writer, payload)
        resp = await _read_response(reader)

        assert resp["status"] == "ok"
        # Server should use the token-derived agent_name, not self-reported
        assert resp["result"]["agent_id_seen"] == agent_name

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_auth_failure_bad_token(tmp_path, token_env):
    """Request with wrong token is rejected and connection closed."""
    mgr, secret, agent_name = token_env
    sock_path = str(tmp_path / "test.sock")
    pid_path = str(tmp_path / "test.pid")

    server = VaireSocketServer(
        socket_path=sock_path,
        pid_file=pid_path,
        dispatch_table=_make_dispatch(),
        token_manager=mgr,
        auth_enabled=True,
    )
    await server.start()

    try:
        reader, writer = await asyncio.open_unix_connection(sock_path)

        payload = {
            "id": "req-1",
            "method": "memory_stats",
            "agent_id": "attacker",
            "auth_token": "invalid-token-value",
            "params": {},
        }
        await write_message(writer, payload)
        resp = await _read_response(reader)

        assert resp["status"] == "error"
        assert resp["code"] == "AUTH_FAILED"

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_auth_failure_missing_token(tmp_path, token_env):
    """Request without auth_token is rejected when tokens exist."""
    mgr, secret, agent_name = token_env
    sock_path = str(tmp_path / "test.sock")
    pid_path = str(tmp_path / "test.pid")

    server = VaireSocketServer(
        socket_path=sock_path,
        pid_file=pid_path,
        dispatch_table=_make_dispatch(),
        token_manager=mgr,
        auth_enabled=True,
    )
    await server.start()

    try:
        reader, writer = await asyncio.open_unix_connection(sock_path)

        payload = {
            "id": "req-1",
            "method": "memory_stats",
            "agent_id": "no-token-agent",
            "params": {},
        }
        await write_message(writer, payload)
        resp = await _read_response(reader)

        assert resp["status"] == "error"
        assert resp["code"] == "AUTH_FAILED"

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_auth_disabled(tmp_path, token_env):
    """When auth is disabled, unauthenticated requests are accepted."""
    mgr, secret, agent_name = token_env
    sock_path = str(tmp_path / "test.sock")
    pid_path = str(tmp_path / "test.pid")

    server = VaireSocketServer(
        socket_path=sock_path,
        pid_file=pid_path,
        dispatch_table=_make_dispatch(),
        token_manager=mgr,
        auth_enabled=False,  # disabled
    )
    await server.start()

    try:
        reader, writer = await asyncio.open_unix_connection(sock_path)

        payload = {
            "id": "req-1",
            "method": "memory_stats",
            "agent_id": "unauthenticated-agent",
            "params": {},
        }
        await write_message(writer, payload)
        resp = await _read_response(reader)

        assert resp["status"] == "ok"
        assert resp["result"]["agent_id_seen"] == "unauthenticated-agent"

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_migration_grace_period_no_tokens(tmp_path):
    """When auth is enabled but no tokens exist, connections are allowed (migration path)."""
    tokens_dir = tmp_path / "empty-tokens"
    mgr = TokenManager(tokens_dir)
    # Do NOT create any tokens

    sock_path = str(tmp_path / "test.sock")
    pid_path = str(tmp_path / "test.pid")

    server = VaireSocketServer(
        socket_path=sock_path,
        pid_file=pid_path,
        dispatch_table=_make_dispatch(),
        token_manager=mgr,
        auth_enabled=True,
    )
    await server.start()

    try:
        reader, writer = await asyncio.open_unix_connection(sock_path)

        payload = {
            "id": "req-1",
            "method": "memory_stats",
            "agent_id": "legacy-agent",
            "params": {},
        }
        await write_message(writer, payload)
        resp = await _read_response(reader)

        assert resp["status"] == "ok"
        # Self-reported agent_id is used during migration grace period
        assert resp["result"]["agent_id_seen"] == "legacy-agent"

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_auth_no_token_manager(tmp_path):
    """When no token_manager is provided, auth falls through to self-reported identity."""
    sock_path = str(tmp_path / "test.sock")
    pid_path = str(tmp_path / "test.pid")

    server = VaireSocketServer(
        socket_path=sock_path,
        pid_file=pid_path,
        dispatch_table=_make_dispatch(),
        token_manager=None,
        auth_enabled=True,
    )
    await server.start()

    try:
        reader, writer = await asyncio.open_unix_connection(sock_path)

        payload = {
            "id": "req-1",
            "method": "memory_stats",
            "agent_id": "fallback-agent",
            "params": {},
        }
        await write_message(writer, payload)
        resp = await _read_response(reader)

        assert resp["status"] == "ok"
        assert resp["result"]["agent_id_seen"] == "fallback-agent"

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_second_request_uses_latched_identity(tmp_path, token_env):
    """After auth, subsequent requests on the same connection don't need re-auth."""
    mgr, secret, agent_name = token_env
    sock_path = str(tmp_path / "test.sock")
    pid_path = str(tmp_path / "test.pid")

    server = VaireSocketServer(
        socket_path=sock_path,
        pid_file=pid_path,
        dispatch_table=_make_dispatch(),
        token_manager=mgr,
        auth_enabled=True,
    )
    await server.start()

    try:
        reader, writer = await asyncio.open_unix_connection(sock_path)

        # First request: auth
        p1 = {
            "id": "req-1",
            "method": "memory_stats",
            "agent_id": "ignored",
            "auth_token": secret,
            "params": {},
        }
        await write_message(writer, p1)
        r1 = await _read_response(reader)
        assert r1["status"] == "ok"
        assert r1["result"]["agent_id_seen"] == agent_name

        # Second request: no auth_token needed, identity is latched
        p2 = {
            "id": "req-2",
            "method": "memory_stats",
            "agent_id": "still-ignored",
            "params": {},
        }
        await write_message(writer, p2)
        r2 = await _read_response(reader)
        assert r2["status"] == "ok"
        assert r2["result"]["agent_id_seen"] == agent_name

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


# ── VaireClient auth_token test ───────────────────────────────────────────────


class TestClientAuthToken:
    """Test that VaireClient includes auth_token in wire payloads."""

    def test_client_includes_token(self):
        """VaireClient stores auth_token and would include it in payloads."""
        from vaire.socket_client import VaireClient
        client = VaireClient(
            socket_path="/tmp/fake.sock",
            agent_id="test-agent",
            auth_token="my-secret-token",
        )
        assert client._auth_token == "my-secret-token"

    def test_client_no_token(self):
        """VaireClient without auth_token has None."""
        from vaire.socket_client import VaireClient
        client = VaireClient(
            socket_path="/tmp/fake.sock",
            agent_id="test-agent",
        )
        assert client._auth_token is None


# ── Config settings test ──────────────────────────────────────────────────────


class TestConfigSettings:
    """Test that auth settings exist in the Settings model."""

    def test_default_auth_enabled(self):
        from vaire.config import Settings
        s = Settings()
        assert s.SOCKET_AUTH_ENABLED is True

    def test_default_tokens_dir(self):
        from vaire.config import Settings
        s = Settings()
        assert s.SOCKET_AUTH_TOKENS_DIR == "~/.vaire/tokens"

    def test_tokens_dir_resolved(self):
        from vaire.config import Settings
        s = Settings()
        resolved = s.socket_auth_tokens_dir_resolved
        assert isinstance(resolved, Path)
        assert "~" not in str(resolved)


# ── ConnectionState auth field test ───────────────────────────────────────────


class TestConnectionState:
    """Test ConnectionState defaults."""

    def test_default_not_authenticated(self):
        state = ConnectionState()
        assert state.authenticated is False

    def test_authenticated_flag(self):
        state = ConnectionState(authenticated=True)
        assert state.authenticated is True
