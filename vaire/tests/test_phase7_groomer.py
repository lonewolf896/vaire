"""Tests for Phase 7: GroomerEngine audit and mutation methods."""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from vaire.config import Settings
from vaire.groomer import GroomerEngine
from vaire.storage import StorageEngine


@pytest.fixture
def settings():
    return Settings(DB_PATH=":memory:")


@pytest.fixture
def storage(tmp_path):
    engine = StorageEngine(str(tmp_path / "groomer_test.db"))
    yield engine
    engine.close()


@pytest.fixture
def embeddings():
    mock = MagicMock()
    # encode_document returns a fake 384-float blob
    mock.encode_document.return_value = b"\x00" * (384 * 4)
    return mock


@pytest.fixture
def groomer(storage, embeddings, settings):
    return GroomerEngine(
        storage=storage,
        embeddings=embeddings,
        cache=None,   # No cache — simpler for unit tests
        settings=settings,
    )


def _insert(storage: StorageEngine, content: str, heat: float = 1.0, tags=None, directory="/tmp") -> int:
    """Insert a memory and return its id."""
    return storage.insert_memory({
        "content": content,
        "embedding": None,
        "tags": tags or [],
        "directory_context": directory,
        "heat": heat,
    })


# ── TestAuditTools ─────────────────────────────────────────────────────────────


class TestAuditTools:
    def test_audit_returns_list(self, groomer, storage):
        _insert(storage, "Hello world memory content here.")
        result = groomer.audit()
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_audit_filters_by_directory(self, groomer, storage):
        _insert(storage, "In alpha", directory="/alpha")
        _insert(storage, "In beta", directory="/beta")
        result = groomer.audit(directory="/alpha")
        assert all(r["directory"] == "/alpha" for r in result)

    def test_audit_result_has_summary_fields(self, groomer, storage):
        _insert(storage, "Check the summary fields are present here.")
        result = groomer.audit()
        assert len(result) >= 1
        r = result[0]
        assert "id" in r
        assert "content_preview" in r
        assert "heat" in r
        assert "tags" in r

    def test_inspect_returns_memory(self, groomer, storage):
        mid = _insert(storage, "Inspect this memory content.")
        result = groomer.inspect(mid)
        assert result["id"] == mid
        assert "content" in result

    def test_inspect_unknown_id_returns_error(self, groomer, storage):
        result = groomer.inspect(99999)
        assert "error" in result

    def test_inspect_strips_embedding(self, groomer, storage):
        mid = _insert(storage, "Should not return raw embedding bytes.")
        result = groomer.inspect(mid)
        assert "embedding" not in result

    def test_find_orphans_returns_cold_untagged(self, groomer, storage):
        _insert(storage, "Cold orphan memory without tags.", heat=0.1, tags=[])
        _insert(storage, "Hot tagged memory is not orphan.", heat=0.9, tags=["project"])
        orphans = groomer.find_orphans()
        ids = {r["id"] for r in orphans}
        # The cold untagged one should appear in orphans
        all_mems = storage.get_memories_by_filter(limit=100)
        cold_ids = {m["id"] for m in all_mems if m["heat"] < 0.15 and not m.get("tags")}
        assert cold_ids <= ids

    def test_find_stale_returns_only_stale(self, groomer, storage):
        mid = _insert(storage, "Stale memory with invalid file path.")
        storage.update_memory_staleness(mid, True)
        stale = groomer.find_stale()
        stale_ids = [r["id"] for r in stale]
        assert mid in stale_ids

    def test_get_stats_returns_expected_keys(self, groomer, storage):
        _insert(storage, "Stats test memory content.")
        stats = groomer.get_stats()
        assert "total_memories" in stats
        assert "stale_count" in stats
        assert "orphan_count" in stats
        assert "duplicate_count" in stats
        assert "heat_distribution" in stats

    def test_find_contradictions_returns_list(self, groomer, storage):
        _insert(storage, "We use Python for all services.")
        _insert(storage, "We don't use Python, we switched to Go.")
        result = groomer.find_contradictions()
        assert isinstance(result, list)

    def test_find_contradictions_skips_long_memories(self, groomer, storage):
        """Long multi-topic memories (>2000 chars) should be excluded."""
        long_content = "We use Python. " * 200  # ~3000 chars, multi-topic reference doc
        _insert(storage, long_content)
        _insert(storage, "We don't use Python, we prefer Go for services.")
        result = groomer.find_contradictions()
        # Long memory should be skipped entirely — no pairs involving it
        assert all(
            len(r["content_a"]) <= 2000 and len(r["content_b"]) <= 2000
            for r in result
        )

    def test_find_contradictions_requires_shared_ratio(self, groomer, storage):
        """Two memories sharing only common words but different topics should not match."""
        # These share "deploy" and "kubernetes" but are about very different things
        _insert(storage, "We deploy kubernetes pods using helm charts on the production cluster every week.")
        _insert(storage, "We don't deploy kubernetes operators manually; we use gitops automation instead.")
        result = groomer.find_contradictions()
        # With the shared ratio check, these may still match (high overlap).
        # But unrelated topics sharing just 2-3 generic words should not.
        _insert(storage, "The coffee machine in the break room uses filtered water from the tap system.")
        _insert(storage, "We don't use the elevator during fire drills according to safety protocol.")
        result2 = groomer.find_contradictions()
        # Coffee machine vs fire drill — minimal topical overlap despite sharing "use"
        unrelated_pairs = [
            r for r in result2
            if ("coffee" in r["content_a"] and "elevator" in r["content_b"])
            or ("elevator" in r["content_a"] and "coffee" in r["content_b"])
        ]
        assert len(unrelated_pairs) == 0

    def test_find_contradictions_embedding_gate(self, groomer, storage, embeddings):
        """When embeddings exist, low-similarity pairs should be filtered out."""
        import struct
        # Create two memories with embeddings that have low similarity
        low_sim_emb1 = struct.pack(f"{384}f", *([1.0] + [0.0] * 383))
        low_sim_emb2 = struct.pack(f"{384}f", *([0.0] + [1.0] + [0.0] * 382))
        embeddings.similarity.return_value = 0.1  # Very low similarity

        mid1 = storage.insert_memory({
            "content": "We use Python for backend services in our platform.",
            "embedding": low_sim_emb1,
            "tags": [],
            "directory_context": "/tmp",
            "heat": 1.0,
        })
        mid2 = storage.insert_memory({
            "content": "We don't use Python, we prefer Rust for backend work.",
            "embedding": low_sim_emb2,
            "tags": [],
            "directory_context": "/tmp",
            "heat": 1.0,
        })
        result = groomer.find_contradictions()
        # Low embedding similarity should block the pair
        pair_ids = {(r["memory_id_a"], r["memory_id_b"]) for r in result}
        assert (mid1, mid2) not in pair_ids and (mid2, mid1) not in pair_ids

    def test_find_contradictions_real_contradiction_detected(self, groomer, storage, embeddings):
        """Real contradictions with high embedding similarity should still be caught."""
        import struct
        high_sim_emb = struct.pack(f"{384}f", *([1.0] + [0.0] * 383))
        embeddings.similarity.return_value = 0.9  # High similarity

        mid1 = storage.insert_memory({
            "content": "We use Python for all backend services in the platform.",
            "embedding": high_sim_emb,
            "tags": [],
            "directory_context": "/tmp",
            "heat": 1.0,
        })
        mid2 = storage.insert_memory({
            "content": "We don't use Python for backend services, we switched to Go.",
            "embedding": high_sim_emb,
            "tags": [],
            "directory_context": "/tmp",
            "heat": 1.0,
        })
        result = groomer.find_contradictions()
        pair_ids = {(r["memory_id_a"], r["memory_id_b"]) for r in result}
        assert (mid1, mid2) in pair_ids or (mid2, mid1) in pair_ids


# ── TestMutationTools ──────────────────────────────────────────────────────────


class TestMutationTools:
    def test_retag_updates_tags(self, groomer, storage):
        mid = _insert(storage, "Memory to retag here.", tags=["old"])
        result = groomer.retag(mid, ["new", "updated"])
        assert result["memory_id"] == mid
        assert result["new_tags"] == ["new", "updated"]
        mem = storage.get_memory(mid)
        assert set(mem["tags"]) == {"new", "updated"}

    def test_retag_unknown_id_returns_error(self, groomer, storage):
        result = groomer.retag(99999, ["tag"])
        assert "error" in result

    def test_reclassify_changes_directory(self, groomer, storage):
        mid = _insert(storage, "Memory to reclassify.", directory="/old")
        result = groomer.reclassify(mid, "/new")
        assert result["old_directory"] == "/old"
        assert result["new_directory"] == "/new"
        mem = storage.get_memory(mid)
        assert mem["directory_context"] == "/new"

    def test_update_content_rewrites_memory(self, groomer, storage, embeddings):
        mid = _insert(storage, "Original content text.")
        result = groomer.update_content(mid, "Rewritten content text.")
        assert result["memory_id"] == mid
        mem = storage.get_memory(mid)
        assert "Rewritten" in mem["content"]
        embeddings.encode_document.assert_called()

    def test_promote_sets_max_heat_and_protected(self, groomer, storage):
        mid = _insert(storage, "Promote this memory.", heat=0.3)
        result = groomer.promote(mid)
        assert result["status"] == "promoted"
        mem = storage.get_memory(mid)
        assert mem["heat"] == 1.0
        assert mem["is_protected"] == 1

    def test_demote_sets_low_heat_and_unprotected(self, groomer, storage):
        mid = _insert(storage, "Demote this memory.", heat=0.9)
        result = groomer.demote(mid)
        assert result["status"] == "demoted"
        mem = storage.get_memory(mid)
        assert mem["heat"] < 0.1
        assert mem["is_protected"] == 0

    def test_bulk_delete_removes_matching(self, groomer, storage):
        _insert(storage, "Delete me cold memory.", heat=0.05, directory="/del")
        _insert(storage, "Keep me hot memory.", heat=0.9, directory="/keep")
        result = groomer.bulk_delete({"directory": "/del"})
        assert "deleted_count" in result
        assert result["deleted_count"] >= 1
        remaining = storage.get_memories_by_filter(directory="/del", limit=100)
        assert len(remaining) == 0

    def test_bulk_delete_empty_filter_returns_error(self, groomer, storage):
        result = groomer.bulk_delete({})
        assert "error" in result

    def test_split_creates_new_and_archives_original(self, groomer, storage, embeddings):
        mid = _insert(storage, "Long memory to split into parts.", heat=1.0)
        result = groomer.split(
            mid,
            splits=[
                {"content": "Part one content here.", "tags": ["part1"]},
                {"content": "Part two content here.", "tags": ["part2"]},
            ],
        )
        assert result["archived_id"] == mid
        assert len(result["new_memory_ids"]) == 2
        # Original should be gone
        assert storage.get_memory(mid) is None

    def test_merge_creates_new_archives_old(self, groomer, storage, embeddings):
        mid1 = _insert(storage, "First memory content to merge.", tags=["a"])
        mid2 = _insert(storage, "Second memory content to merge.", tags=["b"])
        result = groomer.merge([mid1, mid2], "Merged content text.", ["a", "b"])
        assert "new_memory_id" in result
        assert set(result["archived_ids"]) == {mid1, mid2}
        # Originals should be gone
        assert storage.get_memory(mid1) is None
        assert storage.get_memory(mid2) is None

    def test_merge_requires_at_least_two_ids(self, groomer, storage):
        mid = _insert(storage, "Only one memory.")
        result = groomer.merge([mid], "Cannot merge one.", [])
        assert "error" in result


# ── TestAutoGroom ──────────────────────────────────────────────────────────────


class TestAutoGroom:
    def test_auto_groom_light_returns_report(self, groomer, storage):
        _insert(storage, "Normal memory for auto groom test.")
        report = groomer.auto_groom(depth="light")
        assert "depth" in report
        assert report["depth"] == "light"
        assert "found_duplicates" in report
        assert "found_stale" in report
        assert "found_orphans" in report
        assert "auto_executed" in report
        assert "recommendations" in report

    def test_auto_groom_medium_includes_contradictions(self, groomer, storage):
        _insert(storage, "We use Docker for deployment.")
        _insert(storage, "We don't use Docker, we switched to Podman.")
        report = groomer.auto_groom(depth="medium")
        assert "found_contradictions" in report

    def test_auto_groom_deep_includes_deep_recommendation(self, groomer, storage):
        _insert(storage, "Deep groom test memory content.")
        report = groomer.auto_groom(depth="deep")
        assert any("Deep mode" in r for r in report["recommendations"])
