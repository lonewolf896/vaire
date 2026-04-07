"""Static reference document system.

Loads and serves immutable reference documents (NIST standards, directives, etc.)
from repo-baked files via a manifest-driven approach.

Security:
- Topics are manifest keys only — no path construction from user input
- All paths jail-checked against reference root
- Directive files hash-verified on every load
- Every directive load audit-logged with agent_id
"""

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from vaire.config import Settings

logger = logging.getLogger(__name__)

# ── Path security ────────────────────────────────────────────────────────────


class PathSecurityError(Exception):
    """Raised on path traversal or integrity failure."""


def verify_path_jail(target: Path, jail: Path) -> Path:
    """Ensure resolved target is within jail directory.

    Returns: resolved target path
    Raises: PathSecurityError if target escapes jail
    """
    resolved = target.resolve()
    jail_resolved = jail.resolve()

    if not resolved.is_relative_to(jail_resolved):
        raise PathSecurityError("Path traversal blocked")

    return resolved


def verify_file_hash(path: Path, expected_hash: str) -> bool:
    """Verify SHA256 hash of file content.

    Args:
        path: File to hash
        expected_hash: Expected hash in format "sha256:<hex>"

    Returns: True if match
    """
    if not expected_hash.startswith("sha256:"):
        raise ValueError(f"Unsupported hash format: {expected_hash}")

    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    expected = expected_hash[7:]
    return actual == expected


def compute_file_hash(path: Path) -> str:
    """Compute SHA256 hash of file. Returns 'sha256:<hex>'."""
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


# ── Manifest schema constants ────────────────────────────────────────────────

_MANIFEST_REQUIRED_KEYS = {"schema_version", "categories", "references"}
_REF_REQUIRED_KEYS = {
    "path", "category", "description", "keywords", "content_hash", "updated",
}
_REF_OPTIONAL_KEYS = {"sections"}
_CAT_REQUIRED_KEYS = {"description", "integrity"}
EXPECTED_SCHEMA_VERSION = 1


# ── ReferenceLoader ──────────────────────────────────────────────────────────


class ReferenceLoader:
    """Loads and serves static reference documents from repo-baked files."""

    def __init__(self, settings: Settings):
        self._root = settings.reference_path_resolved
        self._manifest_path = settings.reference_manifest_resolved
        self._manifest: dict = {}
        self._loaded = False

    def load_manifest(self) -> None:
        """Load and validate manifest from disk. Called once at startup."""
        if not self._manifest_path.exists():
            logger.warning(
                "Reference manifest not found at %s", self._manifest_path
            )
            self._manifest = {
                "schema_version": 1, "categories": {}, "references": {},
            }
            self._loaded = True
            return

        raw = json.loads(self._manifest_path.read_text())
        self._validate_manifest(raw)
        self._verify_all_paths(raw)
        self._manifest = raw
        self._loaded = True
        logger.info(
            "Reference manifest loaded: %d references in %d categories",
            len(raw.get("references", {})),
            len(raw.get("categories", {})),
        )

    def _validate_manifest(self, raw: dict) -> None:
        """Strict schema validation. Rejects unknown keys."""
        unknown_top = set(raw.keys()) - _MANIFEST_REQUIRED_KEYS
        if unknown_top:
            raise ValueError(f"Unknown manifest keys: {unknown_top}")
        for key in _MANIFEST_REQUIRED_KEYS:
            if key not in raw:
                raise ValueError(f"Missing manifest key: {key}")

        if raw["schema_version"] != EXPECTED_SCHEMA_VERSION:
            raise ValueError(
                f"Unexpected schema version: {raw['schema_version']}"
            )

        for cat_name, cat in raw["categories"].items():
            unknown = set(cat.keys()) - _CAT_REQUIRED_KEYS
            if unknown:
                raise ValueError(
                    f"Unknown category keys in '{cat_name}': {unknown}"
                )
            for key in _CAT_REQUIRED_KEYS:
                if key not in cat:
                    raise ValueError(
                        f"Category '{cat_name}' missing key: {key}"
                    )
            if cat["integrity"] not in ("required", "optional"):
                raise ValueError(
                    f"Category '{cat_name}' integrity must be "
                    "'required' or 'optional'"
                )

        for ref_name, ref in raw["references"].items():
            allowed = _REF_REQUIRED_KEYS | _REF_OPTIONAL_KEYS
            unknown = set(ref.keys()) - allowed
            if unknown:
                raise ValueError(
                    f"Unknown reference keys in '{ref_name}': {unknown}"
                )
            for key in _REF_REQUIRED_KEYS:
                if key not in ref:
                    raise ValueError(
                        f"Reference '{ref_name}' missing key: {key}"
                    )
            if ref["category"] not in raw["categories"]:
                raise ValueError(
                    f"Reference '{ref_name}' has unknown category: "
                    f"{ref['category']}"
                )

    def _verify_all_paths(self, raw: dict) -> None:
        """Verify every reference file exists and is within jail."""
        for ref_name, ref in raw["references"].items():
            target = self._root / ref["path"]
            verify_path_jail(target, self._root)
            if not target.exists():
                raise FileNotFoundError(
                    f"Reference file missing: {ref_name} -> {ref['path']}"
                )

    def list_references(self, category: str | None = None) -> dict:
        """Return manifest index (topics, descriptions, categories).

        Does NOT return file content — just metadata for discovery.
        """
        refs = {}
        for topic, entry in self._manifest.get("references", {}).items():
            if category and entry["category"] != category:
                continue
            refs[topic] = {
                "description": entry["description"],
                "category": entry["category"],
                "keywords": entry["keywords"],
                "sections": entry.get("sections", []),
                "updated": entry["updated"],
            }
        return {
            "categories": {
                name: cat["description"]
                for name, cat in self._manifest.get("categories", {}).items()
                if not category or name == category
            },
            "references": refs,
        }

    def load(
        self,
        topic: str,
        section: str | None = None,
        agent_id: str = "",
    ) -> str:
        """Load reference content by topic key.

        Security flow:
        1. Topic must exist in manifest (no path construction from input)
        2. Resolve file path, jail-check against reference root
        3. If category integrity == "required": verify hash on every load
        4. If directive: audit log the access
        5. If section specified: extract section (exact header match)
        """
        if not self._loaded:
            raise RuntimeError(
                "Manifest not loaded — call load_manifest() first"
            )

        entry = self._manifest.get("references", {}).get(topic)
        if entry is None:
            available = list(self._manifest.get("references", {}).keys())
            raise KeyError(
                f"Unknown reference topic: {topic!r}. "
                f"Use load_reference(show_index=True) to see available topics. "
                f"Available: {available[:10]}"
            )

        target = verify_path_jail(self._root / entry["path"], self._root)

        if not target.exists():
            raise FileNotFoundError(
                f"Reference file missing: {topic} -> {entry['path']}"
            )

        cat = self._manifest["categories"][entry["category"]]
        if cat["integrity"] == "required":
            if not verify_file_hash(target, entry["content_hash"]):
                logger.critical(
                    "INTEGRITY FAILURE: Reference %r hash mismatch "
                    "(agent=%s)",
                    topic, agent_id,
                )
                raise PathSecurityError(
                    f"Integrity check failed for {topic}"
                )

        if topic.startswith("directive:"):
            logger.info(
                "DIRECTIVE LOAD: topic=%r agent=%s", topic, agent_id
            )

        content = target.read_text()

        if section:
            content = self._extract_section(content, section)

        return content

    def _extract_section(self, content: str, section: str) -> str:
        """Extract content between ## section header and next ## header.

        Uses word-boundary matching to avoid 'AC-1' matching 'AC-10'.
        Searches for ## headers where the section identifier appears as a
        distinct token (bounded by non-alphanumeric chars or line boundaries).
        """
        lines = content.split("\n")
        start_idx = None
        # Build a pattern that matches the section as a word boundary
        # e.g. "AC-1" matches "## AC-1: Policy" but not "## AC-10: ..."
        section_pattern = re.compile(
            r"(?<![A-Za-z0-9])" + re.escape(section) + r"(?![A-Za-z0-9])",
            re.IGNORECASE,
        )

        for i, line in enumerate(lines):
            if line.startswith("## ") and section_pattern.search(line):
                start_idx = i
                break

        if start_idx is None:
            raise KeyError(f"Section {section!r} not found in document")

        end_idx = len(lines)
        for i in range(start_idx + 1, len(lines)):
            if lines[i].startswith("## ") or lines[i].startswith("# "):
                end_idx = i
                break

        return "\n".join(lines[start_idx:end_idx]).strip()

    def verify_health(self) -> dict:
        """Health check: verify all files exist and hashes match.

        Called by Vale wake cycle. Returns {topic: status} dict.
        """
        results = {}
        for topic, entry in self._manifest.get("references", {}).items():
            try:
                target = verify_path_jail(
                    self._root / entry["path"], self._root
                )
                if not target.exists():
                    results[topic] = "MISSING"
                    continue
                if verify_file_hash(target, entry["content_hash"]):
                    results[topic] = "OK"
                else:
                    results[topic] = "HASH_MISMATCH"
            except Exception as e:
                results[topic] = f"ERROR: {e}"
        return results

    def check_staleness(self, max_age_days: int = 180) -> list[dict]:
        """Check for references older than max_age_days.

        Returns list of stale references for creator review.
        """
        stale = []
        now = datetime.now(timezone.utc)
        cutoff_days = max_age_days

        for topic, entry in self._manifest.get("references", {}).items():
            try:
                updated = datetime.strptime(
                    entry["updated"], "%Y-%m-%d"
                ).replace(tzinfo=timezone.utc)
                age_days = (now - updated).days
                if age_days > cutoff_days:
                    stale.append({
                        "topic": topic,
                        "category": entry["category"],
                        "last_updated": entry["updated"],
                        "age_days": age_days,
                    })
            except (ValueError, KeyError):
                stale.append({
                    "topic": topic,
                    "category": entry.get("category", "unknown"),
                    "last_updated": "INVALID",
                    "age_days": -1,
                })
        return stale
