"""
Token-based authentication for the Vaire Unix domain socket server.

Each agent is issued a token file: ``<tokens_dir>/<agent-name>.token``
containing a random hex secret.  On connection the client sends this
secret; the server validates it and derives the agent_id from the
matching file name (not self-reported).

Token lifecycle:
  - ``create(agent_name)`` → generates a new token file, returns the secret
  - ``validate(secret)``   → returns the agent_name if valid, else None
  - ``revoke(agent_name)`` → deletes the token file
  - ``list_tokens()``      → returns metadata for all token files
"""
from __future__ import annotations

import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Token length: 32 hex chars = 128 bits of entropy
TOKEN_BYTES = 32


@dataclass
class TokenInfo:
    """Metadata about a token file (no secret exposed)."""

    agent_name: str
    created_at: float  # mtime of the token file
    token_path: str


class TokenManager:
    """CRUD operations for file-backed agent tokens.

    Parameters
    ----------
    tokens_dir:
        Directory where ``<agent-name>.token`` files are stored.
        Created on first ``create()`` call if it does not exist.
    """

    def __init__(self, tokens_dir: str | Path) -> None:
        self._tokens_dir = Path(tokens_dir).expanduser()
        # In-memory lookup: secret → agent_name.  Populated lazily.
        self._cache: dict[str, str] | None = None
        self._cache_mtime: float = 0.0

    @property
    def tokens_dir(self) -> Path:
        return self._tokens_dir

    # ── Public API ────────────────────────────────────────────────────────────

    def create(self, agent_name: str) -> str:
        """Generate a new token for *agent_name*; return the hex secret.

        Overwrites any existing token for the same agent name.

        Raises
        ------
        ValueError
            If *agent_name* is empty or contains path-separator characters.
        """
        self._validate_agent_name(agent_name)

        self._tokens_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

        secret = secrets.token_hex(TOKEN_BYTES)
        token_path = self._token_path(agent_name)
        token_path.write_text(secret + "\n")
        # Restrict to owner only
        os.chmod(str(token_path), 0o600)

        # Invalidate cache
        self._cache = None

        logger.info("Token created for agent %r → %s", agent_name, token_path)
        return secret

    def validate(self, secret: str) -> str | None:
        """Return the agent_name whose token matches *secret*, or None.

        Uses an in-memory cache that is refreshed when the tokens directory
        modification time changes.
        """
        if not secret or not isinstance(secret, str):
            return None

        secret = secret.strip()
        cache = self._get_cache()
        return cache.get(secret)

    def revoke(self, agent_name: str) -> bool:
        """Delete the token file for *agent_name*.  Returns True if it existed."""
        self._validate_agent_name(agent_name)
        token_path = self._token_path(agent_name)
        if token_path.exists():
            token_path.unlink()
            self._cache = None
            logger.info("Token revoked for agent %r", agent_name)
            return True
        return False

    def list_tokens(self) -> list[TokenInfo]:
        """Return metadata for all token files (secrets are NOT included)."""
        if not self._tokens_dir.is_dir():
            return []

        result: list[TokenInfo] = []
        for path in sorted(self._tokens_dir.glob("*.token")):
            agent_name = path.stem
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            result.append(
                TokenInfo(
                    agent_name=agent_name,
                    created_at=mtime,
                    token_path=str(path),
                )
            )
        return result

    def has_tokens(self) -> bool:
        """Return True if the tokens directory contains at least one .token file."""
        if not self._tokens_dir.is_dir():
            return False
        return any(self._tokens_dir.glob("*.token"))

    # ── Internal ──────────────────────────────────────────────────────────────

    def _token_path(self, agent_name: str) -> Path:
        return self._tokens_dir / f"{agent_name}.token"

    def _get_cache(self) -> dict[str, str]:
        """Return (and lazily rebuild) the secret→agent_name lookup cache."""
        if self._cache is not None and not self._dir_changed():
            return self._cache

        cache: dict[str, str] = {}
        if self._tokens_dir.is_dir():
            for path in self._tokens_dir.glob("*.token"):
                agent_name = path.stem
                try:
                    secret = path.read_text().strip()
                except OSError:
                    continue
                if secret:
                    cache[secret] = agent_name

        self._cache = cache
        try:
            self._cache_mtime = self._tokens_dir.stat().st_mtime
        except OSError:
            self._cache_mtime = 0.0
        return cache

    def _dir_changed(self) -> bool:
        """Check if the tokens directory mtime has changed since last cache build."""
        try:
            current_mtime = self._tokens_dir.stat().st_mtime
        except OSError:
            return True
        return current_mtime != self._cache_mtime

    @staticmethod
    def _validate_agent_name(agent_name: str) -> None:
        """Raise ValueError if agent_name is invalid."""
        if not agent_name or not isinstance(agent_name, str):
            raise ValueError("agent_name must be a non-empty string")
        if "/" in agent_name or "\\" in agent_name or "\0" in agent_name:
            raise ValueError(
                f"agent_name must not contain path separators: {agent_name!r}"
            )
        if agent_name.startswith("."):
            raise ValueError(
                f"agent_name must not start with '.': {agent_name!r}"
            )
        if len(agent_name) > 128:
            raise ValueError(
                f"agent_name too long ({len(agent_name)} > 128): {agent_name!r}"
            )
