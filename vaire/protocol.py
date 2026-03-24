"""
Wire protocol for the Vaire Unix domain socket server.

Message framing: [4 bytes uint32 big-endian length][JSON payload]

Every message — request or response — is a JSON object preceded by a
4-byte unsigned integer (big-endian) that carries the byte length of
the JSON body.
"""
from __future__ import annotations

import asyncio
import json
import struct
from dataclasses import dataclass, field
from typing import Any

# ── Constants ──────────────────────────────────────────────────────────────────

HEADER_SIZE: int = 4  # uint32 big-endian
MAX_MESSAGE_SIZE: int = 4 * 1024 * 1024  # 4 MiB hard cap — rejected before body read

AGENT_ID_MAX_LEN: int = 128
METHOD_MAX_LEN: int = 64

# "default" is the legacy single-agent sentinel; must never appear in multi-agent traffic.
_RESERVED_AGENT_ID: str = "default"


# ── Exceptions ─────────────────────────────────────────────────────────────────

class ProtocolError(Exception):
    """Raised for framing, encoding, size, or validation failures.

    Attributes:
        code: machine-readable error category sent back to the caller.
    """

    def __init__(self, message: str, code: str = "PROTOCOL_ERROR") -> None:
        super().__init__(message)
        self.code = code


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class Request:
    """Decoded inbound request from a client."""

    id: str
    method: str
    agent_id: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Response:
    """Decoded outbound response to a client."""

    id: str
    status: str  # "ok" or "error"
    result: dict[str, Any] | None = None
    error: str | None = None
    code: str | None = None


# ── Wire I/O ───────────────────────────────────────────────────────────────────

async def write_message(writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
    """Encode *payload* as UTF-8 JSON and send it length-prefixed to *writer*."""
    body = json.dumps(payload).encode("utf-8")
    header = struct.pack(">I", len(body))
    writer.write(header + body)
    await writer.drain()


async def read_message(reader: asyncio.StreamReader) -> dict[str, Any]:
    """Read one length-prefixed JSON message from *reader*.

    Raises:
        ProtocolError(code="DISCONNECTED"):      peer closed the connection mid-frame.
        ProtocolError(code="MESSAGE_TOO_LARGE"): declared length > MAX_MESSAGE_SIZE.
        ProtocolError(code="DECODE_ERROR"):      body is not valid UTF-8 JSON.
    """
    # Fix #2 — catch IncompleteReadError on header read; re-raise as ProtocolError.
    try:
        header = await reader.readexactly(HEADER_SIZE)
    except asyncio.IncompleteReadError:
        raise ProtocolError(
            "Connection closed while reading message header",
            code="DISCONNECTED",
        )

    (length,) = struct.unpack(">I", header)

    # Fix #3 — reject oversized messages BEFORE allocating / reading the body.
    if length > MAX_MESSAGE_SIZE:
        raise ProtocolError(
            f"Declared message length {length} bytes exceeds the "
            f"{MAX_MESSAGE_SIZE}-byte limit",
            code="MESSAGE_TOO_LARGE",
        )

    # Fix #2 — catch IncompleteReadError on body read as well.
    try:
        body = await reader.readexactly(length)
    except asyncio.IncompleteReadError:
        raise ProtocolError(
            "Connection closed while reading message body",
            code="DISCONNECTED",
        )

    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(
            f"Failed to decode message body: {exc}", code="DECODE_ERROR"
        )


# ── Response helpers ───────────────────────────────────────────────────────────

def make_ok_response(
    request_id: str | None, result: dict[str, Any]
) -> dict[str, Any]:
    return {"id": request_id, "status": "ok", "result": result}


def make_error_response(
    request_id: str | None,
    error: str,
    code: str = "INTERNAL_ERROR",
) -> dict[str, Any]:
    return {"id": request_id, "status": "error", "error": error, "code": code}


# ── SEC1: Request validation ───────────────────────────────────────────────────

def validate_request(msg: dict[str, Any]) -> None:
    """Validate agent_id and method at the server dispatch boundary (SEC1).

    Raises:
        ProtocolError(code="INVALID_AGENT_ID"): any agent_id rule violation.
        ProtocolError(code="INVALID_METHOD"):   any method rule violation.
    """
    agent_id = msg.get("agent_id")

    if agent_id is None:
        raise ProtocolError(
            "Missing required field: agent_id", code="INVALID_AGENT_ID"
        )
    if not isinstance(agent_id, str):
        raise ProtocolError(
            "agent_id must be a string", code="INVALID_AGENT_ID"
        )
    if not agent_id.strip():
        raise ProtocolError(
            "agent_id must not be empty or whitespace-only",
            code="INVALID_AGENT_ID",
        )
    if agent_id == _RESERVED_AGENT_ID:
        raise ProtocolError(
            f"agent_id '{_RESERVED_AGENT_ID}' is reserved and may not be "
            "used by clients",
            code="INVALID_AGENT_ID",
        )
    if len(agent_id) > AGENT_ID_MAX_LEN:
        raise ProtocolError(
            f"agent_id length {len(agent_id)} exceeds the "
            f"{AGENT_ID_MAX_LEN}-character limit",
            code="INVALID_AGENT_ID",
        )

    method = msg.get("method")

    if method is None:
        raise ProtocolError(
            "Missing required field: method", code="INVALID_METHOD"
        )
    if not isinstance(method, str):
        raise ProtocolError(
            "method must be a string", code="INVALID_METHOD"
        )
    if not method.strip():
        raise ProtocolError(
            "method must not be empty", code="INVALID_METHOD"
        )
    if len(method) > METHOD_MAX_LEN:
        raise ProtocolError(
            f"method length {len(method)} exceeds the "
            f"{METHOD_MAX_LEN}-character limit",
            code="INVALID_METHOD",
        )

    params = msg.get("params")
    if params is not None and not isinstance(params, dict):
        raise ProtocolError(
            "params must be a JSON object or omitted",
            code="INVALID_PARAMS",
        )
