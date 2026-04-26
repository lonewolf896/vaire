"""Tests for SPIFFE SVID validation and ASGI middleware.

These tests do NOT require a running SPIRE agent. They build cert chains
in-process with cryptography and feed them to the validator and ASGI
middleware via a stubbed scope.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
from contextlib import contextmanager

import pytest

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from vaire.svid import (
    REASON_BAD_SIGNATURE,
    REASON_EXPIRED,
    REASON_FOREIGN_TRUST_DOMAIN,
    REASON_MALFORMED_URI,
    REASON_NOT_YET_VALID,
    REASON_NO_URI_SAN,
    REASON_UNTRUSTED_ISSUER,
    SPIFFEID,
    SVIDValidationError,
    extract_spiffe_id_from_cert,
    validate_svid,
)
from vaire.transport_context import transport_ctx


# ── helpers ───────────────────────────────────────────────────────────


def _gen_ca(common_name: str = "TestSPIRE-CA"):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, common_name)]
    )
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _dt.timedelta(days=1))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None), critical=True
        )
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _gen_leaf(
    ca_key,
    ca_cert,
    spiffe_uri: str | None = "spiffe://prod.ilmarin/groomer",
    not_before_offset: _dt.timedelta = _dt.timedelta(minutes=-5),
    not_after_offset: _dt.timedelta = _dt.timedelta(hours=1),
    extra_uris: list[str] | None = None,
):
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "leaf")])
    now = _dt.datetime.now(_dt.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now + not_before_offset)
        .not_valid_after(now + not_after_offset)
    )
    sans: list[x509.GeneralName] = []
    if spiffe_uri is not None:
        sans.append(x509.UniformResourceIdentifier(spiffe_uri))
    if extra_uris:
        sans.extend(x509.UniformResourceIdentifier(u) for u in extra_uris)
    if sans:
        builder = builder.add_extension(
            x509.SubjectAlternativeName(sans), critical=False
        )
    cert = builder.sign(ca_key, hashes.SHA256())
    return leaf_key, cert


def _cert_to_der(cert) -> bytes:
    return cert.public_bytes(serialization.Encoding.DER)


@contextmanager
def _spire_env(monkeypatch, trust_domain="prod.ilmarin"):
    monkeypatch.setenv("SPIRE_TRUST_DOMAIN", trust_domain)
    yield


# ── SPIFFEID parsing ──────────────────────────────────────────────────


class TestSPIFFEIDParse:
    def test_basic(self):
        sid = SPIFFEID.parse("spiffe://prod.ilmarin/groomer")
        assert sid.trust_domain == "prod.ilmarin"
        assert sid.path == "/groomer"
        assert str(sid) == "spiffe://prod.ilmarin/groomer"

    def test_nested_path(self):
        sid = SPIFFEID.parse("spiffe://prod.ilmarin/role/groomer")
        assert sid.path == "/role/groomer"

    def test_missing_scheme(self):
        with pytest.raises(SVIDValidationError) as exc:
            SPIFFEID.parse("https://prod.ilmarin/groomer")
        assert exc.value.reason == REASON_MALFORMED_URI

    def test_empty_path(self):
        with pytest.raises(SVIDValidationError) as exc:
            SPIFFEID.parse("spiffe://prod.ilmarin/")
        assert exc.value.reason == REASON_MALFORMED_URI

    def test_no_path_separator(self):
        with pytest.raises(SVIDValidationError) as exc:
            SPIFFEID.parse("spiffe://prod.ilmarin")
        assert exc.value.reason == REASON_MALFORMED_URI

    def test_query_rejected(self):
        with pytest.raises(SVIDValidationError) as exc:
            SPIFFEID.parse("spiffe://prod.ilmarin/groomer?x=1")
        assert exc.value.reason == REASON_MALFORMED_URI


# ── extract_spiffe_id_from_cert ───────────────────────────────────────


class TestExtractSpiffeID:
    def test_extracts_uri_san(self):
        ca_key, ca_cert = _gen_ca()
        _, leaf = _gen_leaf(ca_key, ca_cert)
        sid = extract_spiffe_id_from_cert(leaf)
        assert sid.trust_domain == "prod.ilmarin"
        assert sid.path == "/groomer"

    def test_no_san_extension(self):
        ca_key, ca_cert = _gen_ca()
        _, leaf = _gen_leaf(ca_key, ca_cert, spiffe_uri=None)
        with pytest.raises(SVIDValidationError) as exc:
            extract_spiffe_id_from_cert(leaf)
        assert exc.value.reason == REASON_NO_URI_SAN

    def test_multiple_uri_sans_rejected(self):
        ca_key, ca_cert = _gen_ca()
        _, leaf = _gen_leaf(
            ca_key, ca_cert,
            spiffe_uri="spiffe://prod.ilmarin/groomer",
            extra_uris=["spiffe://prod.ilmarin/other"],
        )
        with pytest.raises(SVIDValidationError) as exc:
            extract_spiffe_id_from_cert(leaf)
        assert exc.value.reason == REASON_MALFORMED_URI


# ── validate_svid (full chain) ────────────────────────────────────────


class TestValidateSVID:
    def test_happy_path(self, monkeypatch):
        with _spire_env(monkeypatch):
            ca_key, ca_cert = _gen_ca()
            _, leaf = _gen_leaf(ca_key, ca_cert)
            sid = validate_svid(leaf, trust_bundle=[ca_cert])
            assert str(sid) == "spiffe://prod.ilmarin/groomer"

    def test_foreign_trust_domain_rejected(self, monkeypatch):
        with _spire_env(monkeypatch):
            ca_key, ca_cert = _gen_ca()
            _, leaf = _gen_leaf(
                ca_key, ca_cert, spiffe_uri="spiffe://other.example/groomer"
            )
            with pytest.raises(SVIDValidationError) as exc:
                validate_svid(leaf, trust_bundle=[ca_cert])
            assert exc.value.reason == REASON_FOREIGN_TRUST_DOMAIN

    def test_explicit_trust_domain_override(self):
        ca_key, ca_cert = _gen_ca()
        _, leaf = _gen_leaf(
            ca_key, ca_cert, spiffe_uri="spiffe://staging.ilmarin/groomer"
        )
        sid = validate_svid(
            leaf, trust_bundle=[ca_cert], trust_domain="staging.ilmarin"
        )
        assert sid.trust_domain == "staging.ilmarin"

    def test_expired(self, monkeypatch):
        with _spire_env(monkeypatch):
            ca_key, ca_cert = _gen_ca()
            _, leaf = _gen_leaf(
                ca_key, ca_cert,
                not_before_offset=_dt.timedelta(hours=-2),
                not_after_offset=_dt.timedelta(minutes=-10),
            )
            with pytest.raises(SVIDValidationError) as exc:
                validate_svid(leaf, trust_bundle=[ca_cert])
            assert exc.value.reason == REASON_EXPIRED

    def test_not_yet_valid(self, monkeypatch):
        with _spire_env(monkeypatch):
            ca_key, ca_cert = _gen_ca()
            _, leaf = _gen_leaf(
                ca_key, ca_cert,
                not_before_offset=_dt.timedelta(hours=1),
                not_after_offset=_dt.timedelta(hours=2),
            )
            with pytest.raises(SVIDValidationError) as exc:
                validate_svid(leaf, trust_bundle=[ca_cert])
            assert exc.value.reason == REASON_NOT_YET_VALID

    def test_untrusted_issuer(self, monkeypatch):
        with _spire_env(monkeypatch):
            ca_key, ca_cert = _gen_ca("RealCA")
            _, leaf = _gen_leaf(ca_key, ca_cert)
            other_key, other_ca = _gen_ca("OtherCA")
            with pytest.raises(SVIDValidationError) as exc:
                validate_svid(leaf, trust_bundle=[other_ca])
            assert exc.value.reason == REASON_UNTRUSTED_ISSUER

    def test_bad_signature(self, monkeypatch):
        """Cert claims to be issued by CA-A but is signed by CA-B."""
        with _spire_env(monkeypatch):
            real_key, real_ca = _gen_ca("RealCA")
            fake_key, fake_ca = _gen_ca("RealCA")  # same DN, different key
            # leaf is signed by fake_key but has Issuer=real_ca.subject
            _, leaf = _gen_leaf(fake_key, real_ca)
            with pytest.raises(SVIDValidationError) as exc:
                validate_svid(leaf, trust_bundle=[real_ca])
            assert exc.value.reason == REASON_BAD_SIGNATURE

    def test_malformed_uri_san(self, monkeypatch):
        with _spire_env(monkeypatch):
            ca_key, ca_cert = _gen_ca()
            _, leaf = _gen_leaf(
                ca_key, ca_cert, spiffe_uri="spiffe://prod.ilmarin"
            )  # no path
            with pytest.raises(SVIDValidationError) as exc:
                validate_svid(leaf, trust_bundle=[ca_cert])
            assert exc.value.reason == REASON_MALFORMED_URI


# ── SVIDMiddleware (ASGI integration) ─────────────────────────────────


class TestSVIDMiddleware:
    """Exercise the middleware against a stubbed ASGI scope.

    The scope carries a ``_peercert_der`` key — the helper hook in
    ``_peercert_x509_from_scope`` picks it up so we don't need a real TLS
    transport to run end-to-end.
    """

    def _run(self, middleware, scope):
        async def _do():
            sent: list[dict] = []

            async def send(msg):
                sent.append(msg)

            await middleware(scope, None, send)
            return sent

        return asyncio.run(_do())

    def test_valid_svid_sets_caller_spiffe_id(self, monkeypatch, tmp_path):
        from vaire.server import SVIDMiddleware

        ca_key, ca_cert = _gen_ca()
        _, leaf = _gen_leaf(ca_key, ca_cert)
        bundle_path = tmp_path / "bundle.crt"
        bundle_path.write_bytes(
            ca_cert.public_bytes(serialization.Encoding.PEM)
        )
        monkeypatch.setenv("SPIRE_TRUST_BUNDLE_PATH", str(bundle_path))
        monkeypatch.setenv("SPIRE_TRUST_DOMAIN", "prod.ilmarin")

        captured: dict = {}

        async def inner_app(scope, receive, send):
            info = transport_ctx.get()
            captured["spiffe"] = info.caller_spiffe_id
            captured["is_remote"] = info.is_remote

        mw = SVIDMiddleware(inner_app)
        scope = {
            "type": "http",
            "headers": [],
            "client": ("100.64.0.42", 12345),
            "_peercert_der": _cert_to_der(leaf),
        }
        self._run(mw, scope)
        assert captured["is_remote"] is True
        assert captured["spiffe"] == "spiffe://prod.ilmarin/groomer"

    def test_foreign_trust_domain_returns_403(self, monkeypatch, tmp_path):
        from vaire.server import SVIDMiddleware

        ca_key, ca_cert = _gen_ca()
        _, leaf = _gen_leaf(
            ca_key, ca_cert, spiffe_uri="spiffe://other.example/groomer"
        )
        bundle_path = tmp_path / "bundle.crt"
        bundle_path.write_bytes(
            ca_cert.public_bytes(serialization.Encoding.PEM)
        )
        monkeypatch.setenv("SPIRE_TRUST_BUNDLE_PATH", str(bundle_path))
        monkeypatch.setenv("SPIRE_TRUST_DOMAIN", "prod.ilmarin")

        inner_called = False

        async def inner_app(scope, receive, send):
            nonlocal inner_called
            inner_called = True

        mw = SVIDMiddleware(inner_app)
        scope = {
            "type": "http",
            "headers": [],
            "client": ("100.64.0.42", 12345),
            "_peercert_der": _cert_to_der(leaf),
        }
        sent = self._run(mw, scope)
        assert not inner_called
        assert sent[0]["status"] == 403
        assert b"foreign_trust_domain" in sent[1]["body"]

    def test_no_uri_san_passes_through(self, monkeypatch, tmp_path):
        """Legacy CN-only certs must not be rejected by SVIDMiddleware."""
        from vaire.server import SVIDMiddleware

        ca_key, ca_cert = _gen_ca()
        _, leaf = _gen_leaf(ca_key, ca_cert, spiffe_uri=None)
        bundle_path = tmp_path / "bundle.crt"
        bundle_path.write_bytes(
            ca_cert.public_bytes(serialization.Encoding.PEM)
        )
        monkeypatch.setenv("SPIRE_TRUST_BUNDLE_PATH", str(bundle_path))

        captured: dict = {}

        async def inner_app(scope, receive, send):
            info = transport_ctx.get()
            captured["spiffe"] = info.caller_spiffe_id
            captured["is_remote"] = info.is_remote

        mw = SVIDMiddleware(inner_app)
        scope = {
            "type": "http",
            "headers": [],
            "client": ("100.64.0.42", 12345),
            "_peercert_der": _cert_to_der(leaf),
        }
        self._run(mw, scope)
        # SVIDMiddleware passes through; transport_ctx default still applies.
        assert captured["spiffe"] == ""
        assert captured["is_remote"] is False

    def test_lifespan_passes_through(self):
        from vaire.server import SVIDMiddleware

        called = False

        async def inner_app(scope, receive, send):
            nonlocal called
            called = True

        mw = SVIDMiddleware(inner_app)
        self._run(mw, {"type": "lifespan"})
        assert called

    def test_context_resets_after_request(self, monkeypatch, tmp_path):
        from vaire.server import SVIDMiddleware

        ca_key, ca_cert = _gen_ca()
        _, leaf = _gen_leaf(ca_key, ca_cert)
        bundle_path = tmp_path / "bundle.crt"
        bundle_path.write_bytes(
            ca_cert.public_bytes(serialization.Encoding.PEM)
        )
        monkeypatch.setenv("SPIRE_TRUST_BUNDLE_PATH", str(bundle_path))
        monkeypatch.setenv("SPIRE_TRUST_DOMAIN", "prod.ilmarin")

        async def inner_app(scope, receive, send):
            pass

        mw = SVIDMiddleware(inner_app)
        scope = {
            "type": "http",
            "headers": [],
            "client": ("100.64.0.42", 12345),
            "_peercert_der": _cert_to_der(leaf),
        }
        self._run(mw, scope)
        # After request completes, ContextVar must be back to default.
        assert transport_ctx.get().caller_spiffe_id == ""
        assert transport_ctx.get().is_remote is False


class TestSVIDPlusMTLSCoexistence:
    """SVIDMiddleware + MTLSMiddleware in the real ASGI chain order."""

    def _run(self, middleware, scope):
        async def _do():
            await middleware(scope, None, lambda m: asyncio.sleep(0))

        asyncio.run(_do())

    def test_svid_wins_over_legacy_cn_when_both_present(self, monkeypatch, tmp_path):
        """If SVID succeeds, MTLSMiddleware must NOT overwrite the context."""
        from vaire.server import MTLSMiddleware, SVIDMiddleware

        ca_key, ca_cert = _gen_ca()
        _, leaf = _gen_leaf(ca_key, ca_cert)
        bundle_path = tmp_path / "bundle.crt"
        bundle_path.write_bytes(
            ca_cert.public_bytes(serialization.Encoding.PEM)
        )
        monkeypatch.setenv("SPIRE_TRUST_BUNDLE_PATH", str(bundle_path))
        monkeypatch.setenv("SPIRE_TRUST_DOMAIN", "prod.ilmarin")

        captured: dict = {}

        async def inner_app(scope, receive, send):
            info = transport_ctx.get()
            captured["spiffe"] = info.caller_spiffe_id
            captured["cn"] = info.agent_cn

        chain = SVIDMiddleware(MTLSMiddleware(inner_app))
        scope = {
            "type": "http",
            # An X-Vaire-CN header would normally be picked up by
            # MTLSMiddleware, but SVIDMiddleware already set the SPIFFE ID
            # so the inner middleware must short-circuit.
            "headers": [(b"x-vaire-cn", b"halcyon-legacy")],
            "client": ("100.64.0.42", 12345),
            "_peercert_der": _cert_to_der(leaf),
        }
        self._run(chain, scope)
        assert captured["spiffe"] == "spiffe://prod.ilmarin/groomer"
        # MTLSMiddleware must not have overwritten the SPIFFE-authenticated
        # context with the legacy CN header.
        assert captured["cn"] == ""

    def test_legacy_cn_fallback_when_no_svid(self, monkeypatch, tmp_path):
        """No URI SAN ⇒ MTLSMiddleware handles via X-Vaire-CN header."""
        from vaire.server import MTLSMiddleware, SVIDMiddleware

        ca_key, ca_cert = _gen_ca()
        _, leaf = _gen_leaf(ca_key, ca_cert, spiffe_uri=None)

        captured: dict = {}

        async def inner_app(scope, receive, send):
            info = transport_ctx.get()
            captured["spiffe"] = info.caller_spiffe_id
            captured["cn"] = info.agent_cn
            captured["is_remote"] = info.is_remote

        chain = SVIDMiddleware(MTLSMiddleware(inner_app))
        scope = {
            "type": "http",
            "headers": [(b"x-vaire-cn", b"halcyon-legacy")],
            "client": ("100.64.0.42", 12345),
            "_peercert_der": _cert_to_der(leaf),
        }
        self._run(chain, scope)
        assert captured["spiffe"] == ""
        assert captured["cn"] == "halcyon-legacy"
        assert captured["is_remote"] is True
