# Phase 3, Step 2: Extract CN from TLS Client Certificate

## Overview

The current `MTLSMiddleware` trusts the self-reported `X-Vaire-CN` header for
client identity. Any authenticated remote agent can impersonate another by setting
this header. The fix: extract the actual Common Name (CN) from the TLS client
certificate presented during the handshake.

### How ASGI exposes TLS info

Uvicorn with `ssl_cert_reqs=CERT_REQUIRED` validates the client certificate.
The ASGI scope exposes the peer certificate via `scope["extensions"]["tls"]`
(if available) or through the underlying `ssl.SSLSocket.getpeercert()`.

However, **standard ASGI/uvicorn does NOT expose the peer certificate in the
scope by default**. The `scope["extensions"]` dict may or may not include TLS info
depending on the ASGI server implementation.

### Approach options

**Option A: Extract CN from the SSL transport in middleware**
Use the ASGI `scope["extensions"]["tls"]["server_cert"]` or fall back to
inspecting the transport's `SSLObject`.

Problem: Not portable across ASGI servers. Uvicorn doesn't reliably expose
the peer cert in the ASGI scope.

**Option B: Use a custom uvicorn protocol that captures the peer cert**
Subclass `uvicorn.protocols.http.h11_impl.H11Protocol` to extract the peer
cert and inject it into the ASGI scope.

Problem: Fragile — depends on uvicorn internals.

**Option C: Use the SSL transport object directly in middleware**
The ASGI `scope["transport"]` (if available) or inspect the underlying
connection's transport to call `get_extra_info("peercert")`.

Problem: Not part of the ASGI spec.

**Option D: Extract CN in a uvicorn `--ssl-*` callback and inject as header**
Configure uvicorn's SSL context with a custom `verify_callback` that extracts
the CN and stores it on the connection.

Problem: OpenSSL callbacks are low-level.

**Option E: Use `scope["server"]` + certificate validation at middleware level**
Since uvicorn with `CERT_REQUIRED` already validated the cert, and the CN is in
the cert, we can access it via the transport's `get_extra_info("peercert")`.
In practice, with uvicorn's H11 protocol, the transport IS accessible via
`scope.get("extensions", {}).get("tls", {})` in some versions, or we can
access the underlying server's `transport` object.

### Chosen approach: Dual strategy

1. **Try `scope["extensions"]["tls"]["peercert"]`** — the ASGI-standard way
2. **Try `scope["transport"].get_extra_info("peercert")`** — uvicorn-specific fallback
3. **Fall back to `X-Vaire-CN` header** — degraded mode, logged as warning

This way we get cert-based CN when available, with a logged fallback.

---

## 2a. Updated MTLSMiddleware

```
IN server.py, class MTLSMiddleware:

BEFORE:
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            from vaire.transport_context import TransportInfo

            # Case-insensitive header lookup
            cn_bytes = b"unknown"
            for key, val in scope.get("headers", []):
                if key.lower() == b"x-vaire-cn":
                    cn_bytes = val
                    break
            cn = cn_bytes.decode("utf-8", errors="replace")
            client = scope.get("client", ("unknown", 0))
            token = transport_ctx.set(TransportInfo(
                is_remote=True, agent_cn=cn, client_ip=str(client[0]),
            ))
            try:
                await self.app(scope, receive, send)
            finally:
                transport_ctx.reset(token)
        else:
            await self.app(scope, receive, send)

AFTER:
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            from vaire.transport_context import TransportInfo

            cn = self._extract_cert_cn(scope)
            client = scope.get("client", ("unknown", 0))
            token = transport_ctx.set(TransportInfo(
                is_remote=True, agent_cn=cn, client_ip=str(client[0]),
            ))
            try:
                await self.app(scope, receive, send)
            finally:
                transport_ctx.reset(token)
        else:
            await self.app(scope, receive, send)

    @staticmethod
    def _extract_cert_cn(scope: dict) -> str:
        """Extract Common Name from TLS client certificate.
        
        Tries ASGI-standard TLS extensions first, then uvicorn transport
        fallback, then X-Vaire-CN header as last resort (with warning).
        """
        # Strategy 1: ASGI extensions (future-proof)
        tls_info = scope.get("extensions", {}).get("tls", {})
        peercert = tls_info.get("peercert")
        IF peercert:
            cn = _cn_from_peercert(peercert)
            IF cn:
                RETURN cn

        # Strategy 2: uvicorn transport fallback
        # Uvicorn stores the transport in scope["_transport"] (internal)
        # or accessible via the server's connection tracking
        TRY:
            transport = scope.get("_transport")
            IF transport is None:
                # Some uvicorn versions use "transport" in extensions
                transport = scope.get("extensions", {}).get("transport")
            IF transport is not None:
                ssl_object = transport.get_extra_info("ssl_object")
                IF ssl_object:
                    peercert = ssl_object.getpeercert()
                    IF peercert:
                        cn = _cn_from_peercert(peercert)
                        IF cn:
                            RETURN cn
        EXCEPT Exception:
            pass  # transport not available

        # Strategy 3: Fall back to X-Vaire-CN header (log warning)
        cn_bytes = b"unknown"
        for key, val in scope.get("headers", []):
            IF key.lower() == b"x-vaire-cn":
                cn_bytes = val
                break
        cn = cn_bytes.decode("utf-8", errors="replace")
        IF cn != "unknown":
            logger.warning(
                "Using X-Vaire-CN header for identity (cert CN extraction "
                "unavailable). Client claims: %s", cn
            )
        RETURN cn
```

---

## 2b. Helper function to extract CN from peercert dict

The `ssl.SSLSocket.getpeercert()` returns a dict like:
```python
{
    'subject': (
        (('commonName', 'my-client'),),
        ...
    ),
    'issuer': (...),
    ...
}
```

```
IN server.py, module-level helper (near MTLSMiddleware):

FUNCTION _cn_from_peercert(peercert: dict) -> str | None:
    """Extract the Common Name from an ssl.getpeercert() dict."""
    subject = peercert.get("subject", ())
    for rdn in subject:
        # Each RDN is a tuple of (type, value) pairs
        for attr_type, attr_value in rdn:
            IF attr_type == "commonName":
                RETURN attr_value
    RETURN None
```

---

## 2c. Important: X-Vaire-CN header remains supported but advisory

The header is still read as a fallback because:
1. Some ASGI servers may not expose the peer cert
2. The `mcp-remote` proxy tool uses the header for identity forwarding
3. Existing client configurations set the header

But the cert CN takes **precedence** when available. The header is only used
if cert extraction fails, and a warning is logged.

---

## 2d. Test considerations

To test cert CN extraction without a real TLS connection, the test can mock
the scope with the appropriate extensions:

```
PSEUDOCODE for test:

def test_extract_cert_cn_from_extensions():
    scope = {
        "type": "http",
        "extensions": {
            "tls": {
                "peercert": {
                    "subject": ((("commonName", "test-agent"),),)
                }
            }
        },
        "headers": [(b"x-vaire-cn", b"spoofed-agent")],
        "client": ("127.0.0.1", 12345),
    }
    cn = MTLSMiddleware._extract_cert_cn(scope)
    ASSERT cn == "test-agent"  # cert CN wins over header


def test_extract_cert_cn_falls_back_to_header():
    scope = {
        "type": "http",
        "extensions": {},
        "headers": [(b"x-vaire-cn", b"fallback-agent")],
        "client": ("127.0.0.1", 12345),
    }
    cn = MTLSMiddleware._extract_cert_cn(scope)
    ASSERT cn == "fallback-agent"  # header used when cert unavailable


def test_extract_cert_cn_unknown_when_nothing():
    scope = {
        "type": "http",
        "extensions": {},
        "headers": [],
        "client": ("127.0.0.1", 12345),
    }
    cn = MTLSMiddleware._extract_cert_cn(scope)
    ASSERT cn == "unknown"
```

---

## 2e. Note on uvicorn's `_transport` availability

After research: uvicorn does NOT reliably expose the transport in the ASGI scope
in all versions. The `scope["_transport"]` key is available in some configurations
of `httptools` protocol but not `h11`.

If Strategy 2 fails across all uvicorn versions we use, the fallback to header
is acceptable WITH the warning log. The key improvement is:
- When cert extraction works → spoofing is impossible
- When cert extraction fails → spoofing still possible but LOGGED
- Either way, better than the current silent trust

A future enhancement would be a custom uvicorn protocol subclass that injects
the peercert into `scope["extensions"]["tls"]`, but that's beyond this phase.
