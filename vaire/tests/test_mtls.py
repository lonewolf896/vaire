"""Integration tests for mTLS HTTPS server.

Generates temporary certificates and tests TLS handshake behavior.
These tests start a real uvicorn server on a random port.
"""
import asyncio
import os
import ssl
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
import httpx


def _gen_certs(tmpdir: Path) -> dict[str, Path]:
    """Generate CA + server + client certs in tmpdir using openssl."""
    ca_key = tmpdir / "ca.key"
    ca_crt = tmpdir / "ca.crt"
    srv_key = tmpdir / "server.key"
    srv_crt = tmpdir / "server.crt"
    cli_key = tmpdir / "client.key"
    cli_crt = tmpdir / "client.crt"

    def _run(args):
        subprocess.run(args, check=True, capture_output=True)

    # CA (with keyUsage extension required by strict TLS clients)
    _run(["openssl", "genrsa", "-out", str(ca_key), "2048"])
    ca_conf = tmpdir / "ca.cnf"
    ca_conf.write_text(
        "[req]\n"
        "default_bits = 2048\n"
        "prompt = no\n"
        "distinguished_name = dn\n"
        "x509_extensions = v3_ca\n"
        "[dn]\n"
        "CN = TestCA\n"
        "[v3_ca]\n"
        "basicConstraints = critical,CA:TRUE\n"
        "keyUsage = critical,keyCertSign,cRLSign\n"
    )
    _run([
        "openssl", "req", "-new", "-x509", "-key", str(ca_key),
        "-out", str(ca_crt), "-days", "1", "-config", str(ca_conf),
    ])

    # Server
    _run(["openssl", "genrsa", "-out", str(srv_key), "2048"])
    _run([
        "openssl", "req", "-new", "-key", str(srv_key),
        "-out", str(tmpdir / "srv.csr"), "-subj", "/CN=localhost",
    ])

    # Create SAN config for server cert
    san_conf = tmpdir / "san.cnf"
    san_conf.write_text(
        "[v3_req]\n"
        "subjectAltName = @alt_names\n"
        "keyUsage = critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage = serverAuth\n"
        "[alt_names]\n"
        "DNS.1 = localhost\n"
        "IP.1 = 127.0.0.1\n"
    )
    _run([
        "openssl", "x509", "-req", "-in", str(tmpdir / "srv.csr"),
        "-CA", str(ca_crt), "-CAkey", str(ca_key), "-CAcreateserial",
        "-out", str(srv_crt), "-days", "1",
        "-extfile", str(san_conf), "-extensions", "v3_req",
    ])

    # Client
    _run(["openssl", "genrsa", "-out", str(cli_key), "2048"])
    _run([
        "openssl", "req", "-new", "-key", str(cli_key),
        "-out", str(tmpdir / "cli.csr"), "-subj", "/CN=test-agent",
    ])
    cli_conf = tmpdir / "cli.cnf"
    cli_conf.write_text(
        "[v3_req]\n"
        "keyUsage = critical,digitalSignature\n"
        "extendedKeyUsage = clientAuth\n"
    )
    _run([
        "openssl", "x509", "-req", "-in", str(tmpdir / "cli.csr"),
        "-CA", str(ca_crt), "-CAkey", str(ca_key), "-CAcreateserial",
        "-out", str(cli_crt), "-days", "1",
        "-extfile", str(cli_conf), "-extensions", "v3_req",
    ])

    return {
        "ca_crt": ca_crt, "srv_key": srv_key, "srv_crt": srv_crt,
        "cli_key": cli_key, "cli_crt": cli_crt,
    }


class TestCertGeneration:
    """Verify cert generation works."""

    def test_gen_certs(self, tmp_path):
        certs = _gen_certs(tmp_path)
        for name, path in certs.items():
            assert path.exists(), f"{name} not created"
            assert path.stat().st_size > 0, f"{name} is empty"


class TestSSLContext:
    """Test that SSL contexts are properly configured."""

    def test_server_ssl_context(self, tmp_path):
        certs = _gen_certs(tmp_path)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(certs["srv_crt"]), str(certs["srv_key"]))
        ctx.load_verify_locations(str(certs["ca_crt"]))
        ctx.verify_mode = ssl.CERT_REQUIRED
        assert ctx.verify_mode == ssl.CERT_REQUIRED

    def test_client_ssl_context(self, tmp_path):
        certs = _gen_certs(tmp_path)
        ctx = ssl.create_default_context(cafile=str(certs["ca_crt"]))
        ctx.load_cert_chain(str(certs["cli_crt"]), str(certs["cli_key"]))
        assert ctx.verify_mode == ssl.CERT_REQUIRED


class TestMTLSHandshake:
    """Test actual TLS handshake with uvicorn."""

    @pytest.fixture
    def certs(self, tmp_path):
        return _gen_certs(tmp_path)

    def test_valid_client_cert_accepted(self, certs):
        """Client with valid cert signed by CA should connect."""
        import uvicorn
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        async def health(request):
            return JSONResponse({"status": "ok"})

        app = Starlette(routes=[Route("/health", health)])
        port = 18744  # use high port to avoid conflicts

        config = uvicorn.Config(
            app, host="127.0.0.1", port=port,
            ssl_certfile=str(certs["srv_crt"]),
            ssl_keyfile=str(certs["srv_key"]),
            ssl_ca_certs=str(certs["ca_crt"]),
            ssl_cert_reqs=ssl.CERT_REQUIRED,
            log_level="error",
        )

        async def _run():
            server = uvicorn.Server(config)
            task = asyncio.create_task(server.serve())
            try:
                await asyncio.sleep(1.0)
                ssl_ctx = ssl.create_default_context(cafile=str(certs["ca_crt"]))
                ssl_ctx.load_cert_chain(str(certs["cli_crt"]), str(certs["cli_key"]))
                async with httpx.AsyncClient(verify=ssl_ctx) as client:
                    resp = await client.get(f"https://127.0.0.1:{port}/health")
                    assert resp.status_code == 200
                    assert resp.json()["status"] == "ok"
            finally:
                server.should_exit = True
                await task

        asyncio.run(_run())

    def test_no_client_cert_rejected(self, certs):
        """Client without cert should be rejected at TLS layer."""
        import uvicorn
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        async def health(request):
            return JSONResponse({"status": "ok"})

        app = Starlette(routes=[Route("/health", health)])
        port = 18745

        config = uvicorn.Config(
            app, host="127.0.0.1", port=port,
            ssl_certfile=str(certs["srv_crt"]),
            ssl_keyfile=str(certs["srv_key"]),
            ssl_ca_certs=str(certs["ca_crt"]),
            ssl_cert_reqs=ssl.CERT_REQUIRED,
            log_level="error",
        )

        async def _run():
            server = uvicorn.Server(config)
            task = asyncio.create_task(server.serve())
            try:
                await asyncio.sleep(1.0)
                ssl_ctx = ssl.create_default_context(cafile=str(certs["ca_crt"]))
                with pytest.raises((httpx.ConnectError, httpx.ReadError, ssl.SSLError, ConnectionError, OSError)):
                    async with httpx.AsyncClient(verify=ssl_ctx) as client:
                        await client.get(f"https://127.0.0.1:{port}/health")
            finally:
                server.should_exit = True
                await task

        asyncio.run(_run())
