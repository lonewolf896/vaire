"""Transport context for distinguishing local vs remote MCP calls.

The ``transport_ctx`` ContextVar is set by the mTLS ASGI middleware for
HTTPS requests.  MCP tool functions read it to determine whether the
caller is remote (and therefore needs auto-tagging, provenance override,
etc.).  Local Unix-socket calls keep the default ``TransportInfo()``.

ContextVars propagate correctly through ``asyncio.run_in_executor``
which is used by CPU-heavy tools (remember, recall, restore, etc.).
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TransportInfo:
    """Metadata about the transport that originated the current request."""

    is_remote: bool = False
    agent_cn: str = ""       # Client certificate Common Name
    client_ip: str = ""      # Remote IP address


transport_ctx: ContextVar[TransportInfo] = ContextVar(
    "transport_ctx", default=TransportInfo()
)
