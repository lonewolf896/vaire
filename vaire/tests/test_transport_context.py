"""Tests for transport context and remote auto-tagging."""
import asyncio

import pytest

from vaire.transport_context import TransportInfo, transport_ctx


class TestTransportContext:
    """Tests for ContextVar-based transport info."""

    def test_default_is_local(self):
        info = transport_ctx.get()
        assert not info.is_remote
        assert info.agent_cn == ""
        assert info.client_ip == ""

    def test_set_remote(self):
        info = TransportInfo(is_remote=True, agent_cn="remote-host", client_ip="10.0.0.1")
        token = transport_ctx.set(info)
        try:
            current = transport_ctx.get()
            assert current.is_remote
            assert current.agent_cn == "remote-host"
            assert current.client_ip == "10.0.0.1"
        finally:
            transport_ctx.reset(token)

    def test_reset_returns_to_default(self):
        info = TransportInfo(is_remote=True, agent_cn="test")
        token = transport_ctx.set(info)
        transport_ctx.reset(token)
        assert not transport_ctx.get().is_remote

    def test_frozen(self):
        info = TransportInfo(is_remote=True, agent_cn="test")
        with pytest.raises(AttributeError):
            info.agent_cn = "modified"


class TestMTLSMiddleware:
    """Tests for the ASGI middleware that sets transport context."""

    def test_middleware_sets_context(self):
        from vaire.server import MTLSMiddleware

        captured = {}

        async def inner_app(scope, receive, send):
            info = transport_ctx.get()
            captured["is_remote"] = info.is_remote
            captured["agent_cn"] = info.agent_cn

        async def _run():
            middleware = MTLSMiddleware(inner_app)
            scope = {
                "type": "http",
                "headers": [(b"x-vaire-cn", b"remote-host")],
                "client": ("10.0.0.1", 12345),
            }
            await middleware(scope, None, None)

        asyncio.run(_run())
        assert captured["is_remote"] is True
        assert captured["agent_cn"] == "remote-host"

    def test_middleware_resets_after_request(self):
        from vaire.server import MTLSMiddleware

        async def inner_app(scope, receive, send):
            pass

        async def _run():
            middleware = MTLSMiddleware(inner_app)
            scope = {
                "type": "http",
                "headers": [(b"x-vaire-cn", b"test")],
                "client": ("127.0.0.1", 1234),
            }
            await middleware(scope, None, None)
            # After middleware completes, context should be reset
            assert not transport_ctx.get().is_remote

        asyncio.run(_run())

    def test_middleware_passes_lifespan_through(self):
        from vaire.server import MTLSMiddleware

        called = False

        async def inner_app(scope, receive, send):
            nonlocal called
            called = True

        async def _run():
            middleware = MTLSMiddleware(inner_app)
            await middleware({"type": "lifespan"}, None, None)

        asyncio.run(_run())
        assert called

    def test_middleware_default_cn_unknown(self):
        from vaire.server import MTLSMiddleware

        captured = {}

        async def inner_app(scope, receive, send):
            info = transport_ctx.get()
            captured["cn"] = info.agent_cn

        async def _run():
            middleware = MTLSMiddleware(inner_app)
            scope = {
                "type": "http",
                "headers": [],  # no X-Vaire-CN header
                "client": ("127.0.0.1", 1234),
            }
            await middleware(scope, None, None)

        asyncio.run(_run())
        assert captured["cn"] == "unknown"

    def test_middleware_case_insensitive_header(self):
        """X-Vaire-CN header should be matched case-insensitively."""
        from vaire.server import MTLSMiddleware

        captured = {}

        async def inner_app(scope, receive, send):
            info = transport_ctx.get()
            captured["cn"] = info.agent_cn

        async def _run():
            middleware = MTLSMiddleware(inner_app)
            scope = {
                "type": "http",
                "headers": [(b"X-Vaire-CN", b"remote-mixed-case")],
                "client": ("127.0.0.1", 1234),
            }
            await middleware(scope, None, None)

        asyncio.run(_run())
        assert captured["cn"] == "remote-mixed-case"
