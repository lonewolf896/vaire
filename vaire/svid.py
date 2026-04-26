"""SPIFFE SVID parsing and validation.

Hand-rolled minimal validator. Uses ``cryptography`` (already a transitive
dependency) instead of ``py-spiffe`` to keep the dependency tree light and
avoid a Workload-API socket requirement at validation time — Vaire validates
*incoming* peer certificates against a static trust bundle, not by talking
to a SPIRE agent.

Responsibilities:
  * Parse the URI SAN of an x509 peer certificate to extract a SPIFFE ID
    of the form ``spiffe://<trust-domain>/<path>``.
  * Validate that the trust domain matches the configured one (default
    ``prod.ilmarin``; override via ``SPIRE_TRUST_DOMAIN``).
  * Validate the certificate chain against a trust bundle PEM file
    (default ``/etc/spire/agent/bundle.crt``; override via
    ``SPIRE_TRUST_BUNDLE_PATH``). Includes expiry / not-yet-valid checks.

Failure modes are returned as :class:`SVIDValidationError` so callers can
emit structured audit events with a stable ``reason`` string.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import padding, rsa, ec
from cryptography.x509.oid import ExtensionOID

logger = logging.getLogger(__name__)

DEFAULT_TRUST_DOMAIN: Final[str] = "prod.ilmarin"
DEFAULT_TRUST_BUNDLE_PATH: Final[str] = "/etc/spire/agent/bundle.crt"

# Reason strings used in audit events. Keep stable — operators may grep these.
REASON_NO_URI_SAN: Final[str] = "no_uri_san"
REASON_MALFORMED_URI: Final[str] = "malformed_spiffe_uri"
REASON_FOREIGN_TRUST_DOMAIN: Final[str] = "foreign_trust_domain"
REASON_EXPIRED: Final[str] = "expired"
REASON_NOT_YET_VALID: Final[str] = "not_yet_valid"
REASON_UNTRUSTED_ISSUER: Final[str] = "untrusted_issuer"
REASON_BAD_SIGNATURE: Final[str] = "bad_signature"
REASON_BUNDLE_UNAVAILABLE: Final[str] = "bundle_unavailable"


class SVIDValidationError(Exception):
    """Raised when an SVID fails validation. ``reason`` is grep-stable."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class SPIFFEID:
    """A parsed SPIFFE identifier."""

    trust_domain: str
    path: str  # leading slash included, e.g. "/groomer"

    def __str__(self) -> str:
        return f"spiffe://{self.trust_domain}{self.path}"

    @classmethod
    def parse(cls, uri: str) -> "SPIFFEID":
        """Parse a ``spiffe://`` URI into trust-domain and path components.

        Per the SPIFFE-ID spec the URI MUST be ``spiffe://<td>/<path>`` with
        a non-empty path component. Anything else raises
        :class:`SVIDValidationError(REASON_MALFORMED_URI)`.
        """
        if not uri.startswith("spiffe://"):
            raise SVIDValidationError(REASON_MALFORMED_URI, f"missing scheme: {uri!r}")
        rest = uri[len("spiffe://"):]
        if "/" not in rest:
            raise SVIDValidationError(REASON_MALFORMED_URI, f"missing path: {uri!r}")
        td, _, path = rest.partition("/")
        if not td:
            raise SVIDValidationError(REASON_MALFORMED_URI, f"empty trust domain: {uri!r}")
        if not path:
            raise SVIDValidationError(REASON_MALFORMED_URI, f"empty path: {uri!r}")
        # Reject query/fragment components (forbidden by SPIFFE spec).
        if "?" in path or "#" in path:
            raise SVIDValidationError(REASON_MALFORMED_URI, f"query/fragment present: {uri!r}")
        return cls(trust_domain=td, path="/" + path)


def get_trust_domain() -> str:
    """Configured SPIFFE trust domain (env override → default)."""
    return os.environ.get("SPIRE_TRUST_DOMAIN", DEFAULT_TRUST_DOMAIN)


def get_trust_bundle_path() -> Path:
    """Configured trust bundle path (env override → default)."""
    return Path(os.environ.get("SPIRE_TRUST_BUNDLE_PATH", DEFAULT_TRUST_BUNDLE_PATH))


def extract_spiffe_id_from_cert(cert: x509.Certificate) -> SPIFFEID:
    """Extract the SPIFFE ID from a peer certificate's URI SAN.

    Raises :class:`SVIDValidationError` if no URI SAN is present or the URI
    does not parse as a valid SPIFFE ID.
    """
    try:
        san_ext = cert.extensions.get_extension_for_oid(
            ExtensionOID.SUBJECT_ALTERNATIVE_NAME
        )
    except x509.ExtensionNotFound as e:
        raise SVIDValidationError(REASON_NO_URI_SAN, "no SAN extension") from e

    san: x509.SubjectAlternativeName = san_ext.value  # type: ignore[assignment]
    uris = san.get_values_for_type(x509.UniformResourceIdentifier)
    if not uris:
        raise SVIDValidationError(REASON_NO_URI_SAN, "SAN has no URI entries")

    # SPIFFE spec: an SVID has exactly one URI SAN. Take the first; if more
    # than one is present, treat as malformed (defensive — operators should
    # not be issuing such certs).
    if len(uris) > 1:
        raise SVIDValidationError(
            REASON_MALFORMED_URI,
            f"multiple URI SANs not allowed: {uris}",
        )
    return SPIFFEID.parse(uris[0])


def _load_trust_bundle(path: Path) -> list[x509.Certificate]:
    """Load one or more PEM certificates from the trust bundle file."""
    if not path.is_file():
        raise SVIDValidationError(
            REASON_BUNDLE_UNAVAILABLE, f"trust bundle not found: {path}"
        )
    try:
        data = path.read_bytes()
    except OSError as e:
        raise SVIDValidationError(
            REASON_BUNDLE_UNAVAILABLE, f"cannot read trust bundle {path}: {e}"
        ) from e
    try:
        certs = x509.load_pem_x509_certificates(data)
    except ValueError as e:
        raise SVIDValidationError(
            REASON_BUNDLE_UNAVAILABLE, f"trust bundle PEM parse failed: {e}"
        ) from e
    if not certs:
        raise SVIDValidationError(
            REASON_BUNDLE_UNAVAILABLE, "trust bundle empty"
        )
    return certs


def _verify_signature(cert: x509.Certificate, issuer: x509.Certificate) -> None:
    """Verify ``cert`` was signed by ``issuer``.

    Supports RSA (PKCS1v15) and ECDSA — the two algorithms SPIRE issues.
    Anything else is rejected.
    """
    pubkey = issuer.public_key()
    sig_alg_oid = cert.signature_algorithm_oid
    hash_alg = cert.signature_hash_algorithm
    if hash_alg is None:
        raise SVIDValidationError(
            REASON_BAD_SIGNATURE, f"unsupported sig alg {sig_alg_oid.dotted_string}"
        )
    try:
        if isinstance(pubkey, rsa.RSAPublicKey):
            pubkey.verify(
                cert.signature,
                cert.tbs_certificate_bytes,
                padding.PKCS1v15(),
                hash_alg,
            )
        elif isinstance(pubkey, ec.EllipticCurvePublicKey):
            pubkey.verify(
                cert.signature,
                cert.tbs_certificate_bytes,
                ec.ECDSA(hash_alg),
            )
        else:
            raise SVIDValidationError(
                REASON_BAD_SIGNATURE,
                f"unsupported issuer key type: {type(pubkey).__name__}",
            )
    except SVIDValidationError:
        raise
    except Exception as e:  # cryptography raises InvalidSignature; be defensive
        raise SVIDValidationError(REASON_BAD_SIGNATURE, str(e)) from e


def _check_validity_window(cert: x509.Certificate, now: _dt.datetime) -> None:
    """Reject cert if outside its NotBefore/NotAfter window."""
    # Use timezone-aware accessors when available (cryptography >= 42).
    not_before = getattr(cert, "not_valid_before_utc", None) or cert.not_valid_before
    not_after = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
    if not_before.tzinfo is None:
        not_before = not_before.replace(tzinfo=_dt.timezone.utc)
    if not_after.tzinfo is None:
        not_after = not_after.replace(tzinfo=_dt.timezone.utc)
    if now < not_before:
        raise SVIDValidationError(
            REASON_NOT_YET_VALID, f"NotBefore={not_before.isoformat()}"
        )
    if now > not_after:
        raise SVIDValidationError(
            REASON_EXPIRED, f"NotAfter={not_after.isoformat()}"
        )


def validate_svid(
    cert: x509.Certificate,
    *,
    trust_bundle: list[x509.Certificate] | None = None,
    trust_domain: str | None = None,
    now: _dt.datetime | None = None,
) -> SPIFFEID:
    """Validate a peer certificate as a SPIFFE SVID.

    Steps (in order):
      1. Extract the SPIFFE ID from the URI SAN.
      2. Reject foreign trust domains.
      3. Check NotBefore/NotAfter on the leaf.
      4. Find an issuer in the trust bundle by Subject==Issuer DN match.
      5. Verify the leaf signature against the issuer key.
      6. Check the issuer's own validity window.

    On success returns the :class:`SPIFFEID`. On failure raises
    :class:`SVIDValidationError` with a stable ``reason``.
    """
    spiffe_id = extract_spiffe_id_from_cert(cert)

    expected_td = trust_domain or get_trust_domain()
    if spiffe_id.trust_domain != expected_td:
        raise SVIDValidationError(
            REASON_FOREIGN_TRUST_DOMAIN,
            f"got {spiffe_id.trust_domain!r}, expected {expected_td!r}",
        )

    _now = now or _dt.datetime.now(_dt.timezone.utc)
    _check_validity_window(cert, _now)

    bundle = trust_bundle if trust_bundle is not None else _load_trust_bundle(
        get_trust_bundle_path()
    )

    leaf_issuer_dn = cert.issuer
    matching = [c for c in bundle if c.subject == leaf_issuer_dn]
    if not matching:
        raise SVIDValidationError(
            REASON_UNTRUSTED_ISSUER,
            f"no bundle cert with Subject={leaf_issuer_dn.rfc4514_string()}",
        )

    last_err: SVIDValidationError | None = None
    for issuer in matching:
        try:
            _check_validity_window(issuer, _now)
            _verify_signature(cert, issuer)
            return spiffe_id
        except SVIDValidationError as e:
            last_err = e
            continue
    assert last_err is not None
    raise last_err


__all__ = [
    "DEFAULT_TRUST_BUNDLE_PATH",
    "DEFAULT_TRUST_DOMAIN",
    "REASON_BAD_SIGNATURE",
    "REASON_BUNDLE_UNAVAILABLE",
    "REASON_EXPIRED",
    "REASON_FOREIGN_TRUST_DOMAIN",
    "REASON_MALFORMED_URI",
    "REASON_NOT_YET_VALID",
    "REASON_NO_URI_SAN",
    "REASON_UNTRUSTED_ISSUER",
    "SPIFFEID",
    "SVIDValidationError",
    "extract_spiffe_id_from_cert",
    "get_trust_bundle_path",
    "get_trust_domain",
    "validate_svid",
]
