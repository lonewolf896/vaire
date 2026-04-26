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
    """Metadata about the transport that originated the current request.

    Identity precedence used by callers (see ``MTLSMiddleware`` and
    ``SVIDMiddleware`` in :mod:`vaire.server`):

    * ``caller_spiffe_id`` set ⇒ request authenticated via SPIFFE SVID.
    * ``agent_cn`` set         ⇒ request authenticated via legacy CN-only
      mTLS (or X-Vaire-CN header fallback).

    A remote request that reaches a tool handler MUST have at least one of
    these set; absence of both is a 401 condition enforced upstream.
    """

    is_remote: bool = False
    agent_cn: str = ""           # Client certificate Common Name (legacy mTLS)
    client_ip: str = ""          # Remote IP address
    caller_spiffe_id: str = ""   # SPIFFE ID, e.g. "spiffe://prod.ilmarin/groomer"


transport_ctx: ContextVar[TransportInfo] = ContextVar(
    "transport_ctx", default=TransportInfo()
)
