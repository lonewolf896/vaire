"""Tests for the reference document system."""

import json

import pytest

from vaire.reference import (
    PathSecurityError,
    ReferenceLoader,
    compute_file_hash,
    verify_file_hash,
    verify_path_jail,
)


# ── Path security tests ─────────────────────────────────────────────────


class TestVerifyPathJail:
    def test_path_inside_jail(self, tmp_path):
        target = tmp_path / "subdir" / "file.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
        result = verify_path_jail(target, tmp_path)
        assert result == target.resolve()

    def test_path_traversal_blocked(self, tmp_path):
        target = tmp_path / ".." / "etc" / "passwd"
        with pytest.raises(PathSecurityError, match="traversal"):
            verify_path_jail(target, tmp_path)

    def test_symlink_escape_blocked(self, tmp_path):
        link = tmp_path / "link"
        link.symlink_to("/etc")
        target = link / "passwd"
        with pytest.raises(PathSecurityError, match="traversal"):
            verify_path_jail(target, tmp_path)


class TestFileHash:
    def test_compute_and_verify(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("hello world")
        h = compute_file_hash(f)
        assert h.startswith("sha256:")
        assert verify_file_hash(f, h)

    def test_mismatch(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("hello world")
        assert not verify_file_hash(f, "sha256:0000000000000000")

    def test_unsupported_format(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("hello")
        with pytest.raises(ValueError, match="Unsupported"):
            verify_file_hash(f, "md5:abcdef")


# ── ReferenceLoader tests ───────────────────────────────────────────────


@pytest.fixture
def ref_setup(tmp_path):
    """Create a reference directory with manifest and test files."""
    ref_dir = tmp_path / "reference"
    ref_dir.mkdir()
    nist_dir = ref_dir / "nist" / "800-53"
    nist_dir.mkdir(parents=True)
    directives_dir = ref_dir / "directives"
    directives_dir.mkdir()

    # Create test content files
    ac_content = (
        "# Access Control\n\n"
        "## AC-1: Policy and Procedures\nAC-1 content here.\n\n"
        "## AC-2: Account Management\nAC-2 content here.\n\n"
        "## AC-10: Concurrent Sessions\nAC-10 content here.\n"
    )
    ac_file = nist_dir / "AC.md"
    ac_file.write_text(ac_content)
    ac_hash = compute_file_hash(ac_file)

    directive_content = "# All Agents Directive\nDo the right thing.\n"
    directive_file = directives_dir / "all-agents.md"
    directive_file.write_text(directive_content)
    directive_hash = compute_file_hash(directive_file)

    manifest = {
        "schema_version": 1,
        "categories": {
            "directives": {
                "description": "Agent governance",
                "integrity": "required",
            },
            "nist": {
                "description": "NIST standards",
                "integrity": "optional",
            },
        },
        "references": {
            "800-53:AC": {
                "path": "nist/800-53/AC.md",
                "category": "nist",
                "description": "Access Control family",
                "keywords": ["access control", "account management"],
                "sections": ["AC-1", "AC-2", "AC-10"],
                "content_hash": ac_hash,
                "updated": "2026-04-04",
            },
            "directive:all-agents": {
                "path": "directives/all-agents.md",
                "category": "directives",
                "description": "Prime directive for all agents",
                "keywords": ["directive", "governance"],
                "sections": [],
                "content_hash": directive_hash,
                "updated": "2026-04-04",
            },
        },
    }
    (ref_dir / "manifest.json").write_text(json.dumps(manifest))

    return ref_dir


@pytest.fixture
def loader(ref_setup):
    """Create a loaded ReferenceLoader."""
    from unittest.mock import MagicMock
    settings = MagicMock()
    settings.reference_path_resolved = ref_setup
    settings.reference_manifest_resolved = ref_setup / "manifest.json"
    loader = ReferenceLoader(settings)
    loader.load_manifest()
    return loader


class TestReferenceLoaderManifest:
    def test_loads_valid_manifest(self, loader):
        assert loader._loaded

    def test_missing_manifest_creates_empty(self, tmp_path):
        from unittest.mock import MagicMock
        settings = MagicMock()
        settings.reference_path_resolved = tmp_path
        settings.reference_manifest_resolved = tmp_path / "nonexistent.json"
        rl = ReferenceLoader(settings)
        rl.load_manifest()
        assert rl._loaded
        assert rl._manifest["references"] == {}

    def test_rejects_unknown_top_keys(self, tmp_path):
        from unittest.mock import MagicMock
        manifest = {
            "schema_version": 1,
            "categories": {},
            "references": {},
            "extra_key": True,
        }
        mpath = tmp_path / "manifest.json"
        mpath.write_text(json.dumps(manifest))
        settings = MagicMock()
        settings.reference_path_resolved = tmp_path
        settings.reference_manifest_resolved = mpath
        rl = ReferenceLoader(settings)
        with pytest.raises(ValueError, match="Unknown manifest keys"):
            rl.load_manifest()

    def test_rejects_wrong_schema_version(self, tmp_path):
        from unittest.mock import MagicMock
        manifest = {
            "schema_version": 99,
            "categories": {},
            "references": {},
        }
        mpath = tmp_path / "manifest.json"
        mpath.write_text(json.dumps(manifest))
        settings = MagicMock()
        settings.reference_path_resolved = tmp_path
        settings.reference_manifest_resolved = mpath
        rl = ReferenceLoader(settings)
        with pytest.raises(ValueError, match="schema version"):
            rl.load_manifest()


class TestReferenceLoaderLoad:
    def test_load_full_document(self, loader):
        content = loader.load("800-53:AC")
        assert "AC-1" in content
        assert "AC-2" in content

    def test_load_unknown_topic(self, loader):
        with pytest.raises(KeyError, match="Unknown reference topic"):
            loader.load("nonexistent")

    def test_load_before_manifest(self, ref_setup):
        from unittest.mock import MagicMock
        settings = MagicMock()
        settings.reference_path_resolved = ref_setup
        settings.reference_manifest_resolved = ref_setup / "manifest.json"
        rl = ReferenceLoader(settings)
        with pytest.raises(RuntimeError, match="not loaded"):
            rl.load("800-53:AC")

    def test_directive_hash_verified(self, loader):
        content = loader.load("directive:all-agents")
        assert "Do the right thing" in content

    def test_directive_hash_mismatch_raises(self, loader):
        # Tamper with the file
        path = loader._root / "directives" / "all-agents.md"
        path.write_text("TAMPERED CONTENT")
        with pytest.raises(PathSecurityError, match="Integrity"):
            loader.load("directive:all-agents")


class TestSectionExtraction:
    def test_extract_exact_section(self, loader):
        content = loader.load("800-53:AC", section="AC-1")
        assert "AC-1 content here" in content
        assert "AC-2" not in content

    def test_ac1_does_not_match_ac10(self, loader):
        """Verification fix P2-1: substring match must use word boundaries."""
        content = loader.load("800-53:AC", section="AC-1")
        assert "AC-10" not in content
        assert "AC-1 content here" in content

    def test_extract_ac10(self, loader):
        content = loader.load("800-53:AC", section="AC-10")
        assert "AC-10 content here" in content
        assert "AC-1 content here" not in content

    def test_section_not_found(self, loader):
        with pytest.raises(KeyError, match="not found"):
            loader.load("800-53:AC", section="AC-99")


class TestListReferences:
    def test_list_all(self, loader):
        result = loader.list_references()
        assert "800-53:AC" in result["references"]
        assert "directive:all-agents" in result["references"]
        assert "nist" in result["categories"]
        assert "directives" in result["categories"]

    def test_list_by_category(self, loader):
        result = loader.list_references(category="nist")
        assert "800-53:AC" in result["references"]
        assert "directive:all-agents" not in result["references"]
        assert "nist" in result["categories"]
        assert "directives" not in result["categories"]


class TestHealthCheck:
    def test_all_healthy(self, loader):
        results = loader.verify_health()
        assert all(v == "OK" for v in results.values())

    def test_missing_file(self, loader):
        (loader._root / "nist" / "800-53" / "AC.md").unlink()
        results = loader.verify_health()
        assert results["800-53:AC"] == "MISSING"

    def test_hash_mismatch(self, loader):
        (loader._root / "nist" / "800-53" / "AC.md").write_text("changed")
        results = loader.verify_health()
        assert results["800-53:AC"] == "HASH_MISMATCH"


class TestStaleness:
    def test_no_stale(self, loader):
        stale = loader.check_staleness(max_age_days=365)
        assert len(stale) == 0

    def test_all_stale(self, loader):
        stale = loader.check_staleness(max_age_days=0)
        assert len(stale) == 2
