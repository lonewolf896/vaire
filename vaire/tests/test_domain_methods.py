"""Tests for StorageEngine domain methods (Phase 1 refactor).

These tests validate the 38 new domain methods added to StorageEngine
to replace direct _conn access across the codebase. Written before
implementation (TDD) — all tests should fail until the methods are built.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from vaire.storage import StorageEngine


@pytest.fixture
def storage(tmp_path):
    engine = StorageEngine(str(tmp_path / "test_domain.db"))
    yield engine
    engine.close()


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _hours_ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _days_ago(days):
    return _hours_ago(days * 24)


def _insert_memory(storage, content="test memory", directory="/test", heat=1.0,
                    store_type="episodic", tags=None, **kwargs):
    """Helper to insert a memory with optional field overrides."""
    mem = {
        "content": content,
        "directory_context": directory,
        "heat": heat,
        "tags": tags or ["test"],
        "created_at": kwargs.pop("created_at", _now_iso()),
        "last_accessed": kwargs.pop("last_accessed", _now_iso()),
    }
    mid = storage.insert_memory(mem)

    # Set fields that insert_memory doesn't handle directly
    extra = {}
    if store_type != "episodic":
        extra["store_type"] = store_type
    for field in ("importance", "surprise_score", "confidence", "access_count",
                  "is_protected", "compression_level", "content_fidelity",
                  "compressed", "cluster_id", "reconsolidation_count"):
        if field in kwargs:
            extra[field] = kwargs[field]

    if extra:
        parts = []
        vals = []
        for k, v in extra.items():
            parts.append(f"{k} = ?")
            vals.append(int(v) if isinstance(v, bool) else v)
        vals.append(mid)
        storage._test_conn.execute(
            f"UPDATE memories SET {', '.join(parts)} WHERE id = ?", vals
        )
        storage._test_conn.commit()

    return mid


def _insert_entity(storage, name, entity_type="concept", heat=1.0, archived=False):
    eid = storage.insert_entity({
        "name": name, "type": entity_type, "heat": heat,
    })
    if archived:
        storage.archive_entity(eid)
    return eid


def _insert_relationship(storage, src_id, tgt_id, rel_type="co_occurrence",
                          weight=1.0, event_time=None):
    rel = {
        "source_entity_id": src_id,
        "target_entity_id": tgt_id,
        "relationship_type": rel_type,
        "weight": weight,
    }
    rid = storage.insert_relationship(rel)
    # insert_relationship doesn't store event_time; set it manually if needed
    if event_time:
        storage.execute_write(
            "UPDATE relationships SET event_time = ? WHERE id = ?",
            (event_time, rid),
        )
    return rid


# ═══════════════════════════════════════════════════════════════════════
# 1a. Memory counting & stats
# ═══════════════════════════════════════════════════════════════════════

class TestCountMemories:
    def test_count_all(self, storage):
        _insert_memory(storage, "m1")
        _insert_memory(storage, "m2")
        _insert_memory(storage, "m3", heat=0.0)
        # heat > 0 by default, so m3 excluded
        assert storage.count_memories() == 2

    def test_count_by_store_type(self, storage):
        _insert_memory(storage, "ep1", store_type="episodic")
        _insert_memory(storage, "ep2", store_type="episodic")
        _insert_memory(storage, "sem1", store_type="semantic")
        _insert_memory(storage, "ref1", store_type="reference")
        assert storage.count_memories(store_type="episodic") == 2
        assert storage.count_memories(store_type="semantic") == 1
        assert storage.count_memories(store_type="reference") == 1

    def test_count_by_compression_level(self, storage):
        _insert_memory(storage, "c0", compression_level=0)
        _insert_memory(storage, "c1", compression_level=1)
        _insert_memory(storage, "c1b", compression_level=1)
        assert storage.count_memories(compression_level=0) == 1
        assert storage.count_memories(compression_level=1) == 2

    def test_count_with_min_heat(self, storage):
        _insert_memory(storage, "hot", heat=0.9)
        _insert_memory(storage, "warm", heat=0.5)
        _insert_memory(storage, "cold", heat=0.1)
        assert storage.count_memories(min_heat=0.4) == 2
        assert storage.count_memories(min_heat=0.8) == 1

    def test_count_empty_db(self, storage):
        assert storage.count_memories() == 0


class TestSumReconsolidationCount:
    def test_sum(self, storage):
        _insert_memory(storage, "m1", reconsolidation_count=3)
        _insert_memory(storage, "m2", reconsolidation_count=7)
        _insert_memory(storage, "m3")  # default 0
        assert storage.sum_reconsolidation_count() == 10

    def test_sum_empty_db(self, storage):
        assert storage.sum_reconsolidation_count() == 0


class TestCountCausalRelationships:
    def test_count(self, storage):
        e1 = _insert_entity(storage, "a")
        e2 = _insert_entity(storage, "b")
        e3 = _insert_entity(storage, "c")
        r1 = _insert_relationship(storage, e1, e2)
        r2 = _insert_relationship(storage, e2, e3)
        # Mark one as causal
        storage.execute_write(
            "UPDATE relationships SET is_causal = 1 WHERE id = ?", (r1,)
        )
        assert storage.count_causal_relationships() == 1

    def test_count_empty(self, storage):
        assert storage.count_causal_relationships() == 0


# ═══════════════════════════════════════════════════════════════════════
# 1b. Specialized memory queries
# ═══════════════════════════════════════════════════════════════════════

class TestGetMemoriesByStoreType:
    def test_filters_by_type(self, storage):
        _insert_memory(storage, "ep", store_type="episodic")
        _insert_memory(storage, "sem", store_type="semantic")
        result = storage.get_memories_by_store_type("episodic")
        assert len(result) == 1
        assert result[0]["content"] == "ep"

    def test_filters_by_directory(self, storage):
        _insert_memory(storage, "m1", directory="/a", store_type="episodic")
        _insert_memory(storage, "m2", directory="/b", store_type="episodic")
        result = storage.get_memories_by_store_type("episodic", directory="/a")
        assert len(result) == 1
        assert result[0]["content"] == "m1"

    def test_require_embedding(self, storage):
        mid = _insert_memory(storage, "no_emb", store_type="episodic")
        # This memory has no embedding by default
        result = storage.get_memories_by_store_type(
            "episodic", require_embedding=True
        )
        assert len(result) == 0

    def test_limit(self, storage):
        for i in range(5):
            _insert_memory(storage, f"m{i}", store_type="episodic")
        result = storage.get_memories_by_store_type("episodic", limit=3)
        assert len(result) == 3

    def test_excludes_zero_heat(self, storage):
        _insert_memory(storage, "hot", store_type="episodic", heat=1.0)
        _insert_memory(storage, "cold", store_type="episodic", heat=0.0)
        result = storage.get_memories_by_store_type("episodic")
        assert len(result) == 1


class TestGetAnchoredMemories:
    def test_returns_anchored(self, storage):
        mid = _insert_memory(
            storage, "anchored fact",
            tags=["_anchor", "anchor:reason"],
            is_protected=True,
        )
        result = storage.get_anchored_memories()
        assert len(result) >= 1
        assert any(m["id"] == mid for m in result)

    def test_excludes_non_anchored(self, storage):
        _insert_memory(storage, "normal memory", tags=["test"])
        result = storage.get_anchored_memories()
        assert len(result) == 0

    def test_respects_limit(self, storage):
        for i in range(5):
            _insert_memory(
                storage, f"anchor {i}",
                tags=["_anchor"], is_protected=True,
            )
        result = storage.get_anchored_memories(limit=3)
        assert len(result) == 3

    def test_ordered_by_created_at_desc(self, storage):
        m1 = _insert_memory(
            storage, "old", tags=["_anchor"], is_protected=True,
            created_at=_hours_ago(48),
        )
        m2 = _insert_memory(
            storage, "new", tags=["_anchor"], is_protected=True,
            created_at=_hours_ago(1),
        )
        result = storage.get_anchored_memories()
        assert result[0]["id"] == m2  # newer first

    def test_no_wildcard_match_on_xanchor(self, storage):
        """Ensure '_' in _anchor is not treated as SQL wildcard."""
        _insert_memory(
            storage, "not actually anchored",
            tags=["xanchor"],  # _ wildcard would match this
            is_protected=True,
        )
        result = storage.get_anchored_memories()
        assert len(result) == 0


class TestGetRecentMemories:
    def test_returns_non_anchored(self, storage):
        _insert_memory(storage, "recent", tags=["test"])
        result = storage.get_recent_memories()
        assert len(result) == 1

    def test_excludes_anchored_when_flag_set(self, storage):
        _insert_memory(
            storage, "anchor", tags=["_anchor"], is_protected=True,
        )
        _insert_memory(storage, "normal", tags=["test"])
        result = storage.get_recent_memories(exclude_anchored=True)
        assert len(result) == 1
        assert result[0]["content"] == "normal"

    def test_includes_anchored_when_flag_false(self, storage):
        _insert_memory(
            storage, "anchor", tags=["_anchor"], is_protected=True,
        )
        _insert_memory(storage, "normal", tags=["test"])
        result = storage.get_recent_memories(exclude_anchored=False)
        assert len(result) == 2


class TestGetMemoriesForPruning:
    def test_returns_eligible(self, storage):
        mid = _insert_memory(
            storage, "prune me", heat=0.005, confidence=0.2, access_count=0,
        )
        result = storage.get_memories_for_pruning()
        assert any(m["id"] == mid for m in result)

    def test_excludes_accessed(self, storage):
        _insert_memory(
            storage, "accessed", heat=0.005, confidence=0.2, access_count=5,
        )
        result = storage.get_memories_for_pruning()
        assert len(result) == 0

    def test_excludes_reference_type(self, storage):
        _insert_memory(
            storage, "ref", heat=0.005, confidence=0.2, access_count=0,
            store_type="reference",
        )
        result = storage.get_memories_for_pruning()
        assert len(result) == 0


class TestGetMemoriesForStrengthening:
    def test_returns_eligible(self, storage):
        mid = _insert_memory(
            storage, "strengthen me", access_count=10, confidence=0.9,
            importance=0.5,
        )
        result = storage.get_memories_for_strengthening()
        assert any(m["id"] == mid for m in result)

    def test_excludes_low_access(self, storage):
        _insert_memory(
            storage, "not enough", access_count=2, confidence=0.9,
            importance=0.5,
        )
        result = storage.get_memories_for_strengthening()
        assert len(result) == 0

    def test_excludes_reference_type(self, storage):
        _insert_memory(
            storage, "ref", access_count=10, confidence=0.9,
            importance=0.5, store_type="reference",
        )
        result = storage.get_memories_for_strengthening()
        assert len(result) == 0


class TestGetMemoriesInCluster:
    def test_returns_cluster_members(self, storage):
        m1 = _insert_memory(storage, "in cluster", cluster_id=42)
        m2 = _insert_memory(storage, "not in cluster")
        result = storage.get_memories_in_cluster(42)
        assert len(result) == 1
        assert result[0]["id"] == m1

    def test_respects_min_heat(self, storage):
        _insert_memory(storage, "cold", cluster_id=42, heat=0.0)
        result = storage.get_memories_in_cluster(42, min_heat=0.0)
        assert len(result) == 0

    def test_ordered_by_heat_desc(self, storage):
        m1 = _insert_memory(storage, "low", cluster_id=42, heat=0.3)
        m2 = _insert_memory(storage, "high", cluster_id=42, heat=0.9)
        result = storage.get_memories_in_cluster(42)
        assert result[0]["id"] == m2


class TestGetOldVerboseMemories:
    def test_returns_old_long_memories(self, storage):
        long_content = "x " * 600  # > 1000 chars
        mid = _insert_memory(
            storage, long_content, created_at=_days_ago(60),
        )
        result = storage.get_old_verbose_memories(_days_ago(30))
        assert any(m["id"] == mid for m in result)

    def test_excludes_short_memories(self, storage):
        _insert_memory(storage, "short", created_at=_days_ago(60))
        result = storage.get_old_verbose_memories(_days_ago(30))
        assert len(result) == 0

    def test_excludes_already_compressed(self, storage):
        long_content = "x " * 600
        _insert_memory(
            storage, long_content, created_at=_days_ago(60), compressed=True,
        )
        result = storage.get_old_verbose_memories(_days_ago(30))
        assert len(result) == 0


class TestGetMemoryIdsByHeat:
    def test_returns_ids(self, storage):
        m1 = _insert_memory(storage, "hot", heat=0.9)
        m2 = _insert_memory(storage, "cold", heat=0.1)
        result = storage.get_memory_ids_by_heat(0.5, limit=10)
        assert m1 in result
        assert m2 not in result

    def test_respects_limit(self, storage):
        for i in range(10):
            _insert_memory(storage, f"m{i}", heat=0.9)
        result = storage.get_memory_ids_by_heat(0.0, limit=5)
        assert len(result) == 5


class TestGetHdcVector:
    def test_returns_none_when_no_vector(self, storage):
        mid = _insert_memory(storage, "no hdc")
        assert storage.get_hdc_vector(mid) is None

    def test_returns_vector_when_set(self, storage):
        mid = _insert_memory(storage, "has hdc")
        fake_vec = b"\x00" * 40
        storage.execute_write(
            "UPDATE memories SET hdc_vector = ? WHERE id = ?",
            (fake_vec, mid),
        )
        result = storage.get_hdc_vector(mid)
        assert result == fake_vec


class TestFindMemoriesMentioning:
    def test_finds_by_content(self, storage):
        mid = _insert_memory(storage, "The StorageEngine handles queries")
        result = storage.find_memories_mentioning("StorageEngine")
        assert mid in result

    def test_returns_empty_for_no_match(self, storage):
        _insert_memory(storage, "unrelated content")
        result = storage.find_memories_mentioning("NonexistentTerm12345")
        assert len(result) == 0

    def test_excludes_zero_heat(self, storage):
        _insert_memory(storage, "cold mention of Target", heat=0.0)
        result = storage.find_memories_mentioning("Target")
        assert len(result) == 0

    def test_escapes_like_wildcards(self, storage):
        """% and _ in search text should be treated literally."""
        mid = _insert_memory(storage, "100% coverage of _private methods")
        # Should NOT match everything
        result = storage.find_memories_mentioning("100%")
        assert mid in result

    def test_empty_string_returns_empty(self, storage):
        _insert_memory(storage, "some content")
        assert storage.find_memories_mentioning("") == []

    def test_whitespace_only_returns_empty(self, storage):
        _insert_memory(storage, "some content")
        assert storage.find_memories_mentioning("   ") == []


class TestGetHotMemoriesAll:
    def test_returns_hot_memories(self, storage):
        m1 = _insert_memory(storage, "hot", heat=0.9)
        m2 = _insert_memory(storage, "cold", heat=0.0)
        result = storage.get_hot_memories_all()
        ids = [m["id"] for m in result]
        assert m1 in ids
        assert m2 not in ids

    def test_respects_min_heat(self, storage):
        _insert_memory(storage, "warm", heat=0.5)
        _insert_memory(storage, "hot", heat=0.9)
        result = storage.get_hot_memories_all(min_heat=0.7)
        assert len(result) == 1


class TestMemoryExistsWithContent:
    def test_exists(self, storage):
        _insert_memory(storage, "exact content match")
        assert storage.memory_exists_with_content("exact content match")

    def test_not_exists(self, storage):
        assert not storage.memory_exists_with_content("no such content")

    def test_partial_no_match(self, storage):
        _insert_memory(storage, "full content here")
        assert not storage.memory_exists_with_content("full content")


class TestGetLatestMemoryDateMentioning:
    def test_returns_latest_date(self, storage):
        _insert_memory(
            storage, "mentions Target keyword",
            created_at=_hours_ago(48),
        )
        _insert_memory(
            storage, "also mentions Target keyword",
            created_at=_hours_ago(1),
        )
        result = storage.get_latest_memory_date_mentioning("Target")
        assert result is not None
        # The latest should be ~1 hour ago
        dt = datetime.fromisoformat(result)
        assert (datetime.now(timezone.utc) - dt).total_seconds() < 7200

    def test_returns_none_no_match(self, storage):
        assert storage.get_latest_memory_date_mentioning("Nonexistent") is None

    def test_escapes_wildcards(self, storage):
        _insert_memory(storage, "test_method works", created_at=_hours_ago(1))
        # _ should be literal, not wildcard
        result = storage.get_latest_memory_date_mentioning("test_method")
        assert result is not None


# ═══════════════════════════════════════════════════════════════════════
# 1c. Entity queries
# ═══════════════════════════════════════════════════════════════════════

class TestEntityQueries:
    def test_get_entity_name(self, storage):
        eid = _insert_entity(storage, "MyEntity")
        assert storage.get_entity_name(eid) == "MyEntity"

    def test_get_entity_name_not_found(self, storage):
        assert storage.get_entity_name(99999) is None

    def test_get_entity_heat(self, storage):
        eid = _insert_entity(storage, "hot_entity", heat=0.75)
        assert storage.get_entity_heat(eid) == 0.75

    def test_get_entity_heat_not_found(self, storage):
        assert storage.get_entity_heat(99999) is None

    def test_get_entity_by_id(self, storage):
        eid = _insert_entity(storage, "full_entity", entity_type="file")
        result = storage.get_entity_by_id(eid)
        assert result is not None
        assert result["name"] == "full_entity"
        assert result["type"] == "file"

    def test_get_entity_by_id_not_found(self, storage):
        assert storage.get_entity_by_id(99999) is None


# ═══════════════════════════════════════════════════════════════════════
# 1d. Relationship queries
# ═══════════════════════════════════════════════════════════════════════

class TestGetRelationshipsByWeight:
    def test_filters_by_min_weight(self, storage):
        e1 = _insert_entity(storage, "a")
        e2 = _insert_entity(storage, "b")
        e3 = _insert_entity(storage, "c")
        _insert_relationship(storage, e1, e2, weight=3.0)
        _insert_relationship(storage, e2, e3, weight=8.0)
        result = storage.get_relationships_by_weight(5.0)
        assert len(result) == 1
        assert result[0]["weight"] == 8.0

    def test_filters_by_type(self, storage):
        e1 = _insert_entity(storage, "a")
        e2 = _insert_entity(storage, "b")
        _insert_relationship(storage, e1, e2, rel_type="co_occurrence", weight=5.0)
        _insert_relationship(storage, e1, e2, rel_type="causal", weight=5.0)
        result = storage.get_relationships_by_weight(
            1.0, relationship_type="co_occurrence"
        )
        assert len(result) == 1

    def test_includes_null_weight_at_zero(self, storage):
        """NULL weight rows should be included when min_weight=0."""
        e1 = _insert_entity(storage, "a")
        e2 = _insert_entity(storage, "b")
        rid = _insert_relationship(storage, e1, e2, weight=1.0)
        # Set weight to NULL
        storage.execute_write(
            "UPDATE relationships SET weight = NULL WHERE id = ?", (rid,)
        )
        result = storage.get_relationships_by_weight(0.0)
        assert len(result) >= 1


class TestGetRelationshipsAtTime:
    def test_returns_relationships_before_time(self, storage):
        e1 = _insert_entity(storage, "a")
        e2 = _insert_entity(storage, "b")
        _insert_relationship(
            storage, e1, e2, event_time=_hours_ago(48)
        )
        _insert_relationship(
            storage, e1, e2, event_time=_hours_ago(1)
        )
        # Get relationships before 24 hours ago
        result = storage.get_relationships_at_time(e1, _hours_ago(24))
        assert len(result) == 1

    def test_includes_entity_names(self, storage):
        e1 = _insert_entity(storage, "alpha")
        e2 = _insert_entity(storage, "beta")
        _insert_relationship(storage, e1, e2, event_time=_now_iso())
        result = storage.get_relationships_at_time(e1, _now_iso())
        assert len(result) >= 1
        assert "source_name" in result[0]
        assert "target_name" in result[0]


class TestGetRelationshipHistory:
    def test_returns_bidirectional(self, storage):
        e1 = _insert_entity(storage, "a")
        e2 = _insert_entity(storage, "b")
        _insert_relationship(storage, e1, e2)
        _insert_relationship(storage, e2, e1)
        result = storage.get_relationship_history(e1, e2)
        assert len(result) == 2

    def test_includes_names(self, storage):
        e1 = _insert_entity(storage, "alpha")
        e2 = _insert_entity(storage, "beta")
        _insert_relationship(storage, e1, e2)
        result = storage.get_relationship_history(e1, e2)
        assert result[0]["source_name"] == "alpha"


class TestGetAdjacentRelationships:
    def test_returns_all_adjacent(self, storage):
        e1 = _insert_entity(storage, "center")
        e2 = _insert_entity(storage, "neighbor1")
        e3 = _insert_entity(storage, "neighbor2")
        _insert_relationship(storage, e1, e2)
        _insert_relationship(storage, e3, e1)
        result = storage.get_adjacent_relationships(e1)
        assert len(result) == 2

    def test_filters_by_type(self, storage):
        e1 = _insert_entity(storage, "center")
        e2 = _insert_entity(storage, "n1")
        e3 = _insert_entity(storage, "n2")
        _insert_relationship(storage, e1, e2, rel_type="co_occurrence")
        _insert_relationship(storage, e1, e3, rel_type="causal")
        result = storage.get_adjacent_relationships(
            e1, relationship_types=["co_occurrence"]
        )
        assert len(result) == 1

    def test_includes_names(self, storage):
        e1 = _insert_entity(storage, "alpha")
        e2 = _insert_entity(storage, "beta")
        _insert_relationship(storage, e1, e2)
        result = storage.get_adjacent_relationships(e1)
        assert "source_name" in result[0]
        assert "target_name" in result[0]


class TestGetAllRelationshipsForGraph:
    def test_returns_lightweight_dicts(self, storage):
        e1 = _insert_entity(storage, "a")
        e2 = _insert_entity(storage, "b")
        _insert_relationship(storage, e1, e2, weight=5.0)
        result = storage.get_all_relationships_for_graph()
        assert len(result) == 1
        r = result[0]
        assert "source_entity_id" in r
        assert "target_entity_id" in r
        assert "weight" in r


class TestRelationshipExists:
    def test_exists_forward(self, storage):
        e1 = _insert_entity(storage, "a")
        e2 = _insert_entity(storage, "b")
        _insert_relationship(storage, e1, e2)
        assert storage.relationship_exists(e1, e2)

    def test_exists_reverse(self, storage):
        e1 = _insert_entity(storage, "a")
        e2 = _insert_entity(storage, "b")
        _insert_relationship(storage, e1, e2)
        assert storage.relationship_exists(e2, e1)  # bidirectional

    def test_not_exists(self, storage):
        e1 = _insert_entity(storage, "a")
        e2 = _insert_entity(storage, "b")
        assert not storage.relationship_exists(e1, e2)


class TestGetRelationshipsByTypeForEntity:
    def test_returns_matching(self, storage):
        e1 = _insert_entity(storage, "error_obj")
        e2 = _insert_entity(storage, "fix_obj")
        _insert_relationship(storage, e1, e2, rel_type="resolved_by")
        _insert_relationship(storage, e1, e2, rel_type="co_occurrence")
        result = storage.get_relationships_by_type_for_entity(e1, "resolved_by")
        assert len(result) == 1

    def test_returns_empty_when_none(self, storage):
        e1 = _insert_entity(storage, "a")
        result = storage.get_relationships_by_type_for_entity(e1, "nonexistent")
        assert len(result) == 0


class TestGetDistinctRelationshipTypes:
    def test_returns_types(self, storage):
        e1 = _insert_entity(storage, "a")
        e2 = _insert_entity(storage, "b")
        _insert_relationship(storage, e1, e2, rel_type="co_occurrence")
        _insert_relationship(storage, e1, e2, rel_type="causal")
        result = storage.get_distinct_relationship_types()
        assert "co_occurrence" in result
        assert "causal" in result

    def test_empty_db(self, storage):
        assert storage.get_distinct_relationship_types() == []


class TestGetTypedRelationship:
    def test_finds_exact_match(self, storage):
        e1 = _insert_entity(storage, "a")
        e2 = _insert_entity(storage, "b")
        _insert_relationship(storage, e1, e2, rel_type="imports")
        result = storage.get_typed_relationship(e1, e2, "imports")
        assert result is not None
        assert result["relationship_type"] == "imports"

    def test_returns_none_wrong_type(self, storage):
        e1 = _insert_entity(storage, "a")
        e2 = _insert_entity(storage, "b")
        _insert_relationship(storage, e1, e2, rel_type="imports")
        result = storage.get_typed_relationship(e1, e2, "co_occurrence")
        assert result is None

    def test_returns_none_wrong_direction(self, storage):
        e1 = _insert_entity(storage, "a")
        e2 = _insert_entity(storage, "b")
        _insert_relationship(storage, e1, e2, rel_type="imports")
        # Reversed direction should not match
        result = storage.get_typed_relationship(e2, e1, "imports")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════
# 1e. Episode queries
# ═══════════════════════════════════════════════════════════════════════

class TestEpisodeQueries:
    def test_get_all_episode_contents(self, storage):
        storage.insert_episode({
            "session_id": "s1", "directory": "/test",
            "raw_content": "episode one", "timestamp": _hours_ago(2),
        })
        storage.insert_episode({
            "session_id": "s1", "directory": "/test",
            "raw_content": "episode two", "timestamp": _hours_ago(1),
        })
        result = storage.get_all_episode_contents()
        assert len(result) == 2
        assert result[0]["raw_content"] == "episode one"  # ASC order
        assert result[1]["raw_content"] == "episode two"

    def test_get_episodes_since_time(self, storage):
        storage.insert_episode({
            "session_id": "s1", "directory": "/test",
            "raw_content": "old", "timestamp": _hours_ago(48),
        })
        storage.insert_episode({
            "session_id": "s1", "directory": "/test",
            "raw_content": "new", "timestamp": _hours_ago(1),
        })
        result = storage.get_episodes_since_time(_hours_ago(24))
        assert len(result) == 1
        assert result[0]["raw_content"] == "new"

    def test_get_episode_session_id(self, storage):
        eid = storage.insert_episode({
            "session_id": "test-session-42", "directory": "/test",
            "raw_content": "content",
        })
        assert storage.get_episode_session_id(eid) == "test-session-42"

    def test_get_episode_session_id_not_found(self, storage):
        assert storage.get_episode_session_id(99999) is None


# ═══════════════════════════════════════════════════════════════════════
# 1f. Causal DAG queries
# ═══════════════════════════════════════════════════════════════════════

class TestCausalQueries:
    def _setup_dag(self, storage):
        e1 = _insert_entity(storage, "cause_entity")
        e2 = _insert_entity(storage, "effect_entity")
        storage.insert_causal_edge({
            "source_entity_id": e1,
            "target_entity_id": e2,
            "algorithm": "pc",
            "confidence": 0.8,
        })
        return e1, e2

    def test_get_causal_causes(self, storage):
        e1, e2 = self._setup_dag(storage)
        causes = storage.get_causal_causes(e2)
        assert len(causes) == 1
        assert causes[0]["source_name"] == "cause_entity"

    def test_get_causal_effects(self, storage):
        e1, e2 = self._setup_dag(storage)
        effects = storage.get_causal_effects(e1)
        assert len(effects) == 1
        assert effects[0]["target_name"] == "effect_entity"

    def test_causal_causes_empty(self, storage):
        e1 = _insert_entity(storage, "isolated")
        assert storage.get_causal_causes(e1) == []


# ═══════════════════════════════════════════════════════════════════════
# 1g. Cluster queries
# ═══════════════════════════════════════════════════════════════════════

class TestClusterQueries:
    def test_get_child_clusters(self, storage):
        parent = storage.insert_cluster({
            "name": "root", "level": 0, "summary": "top",
        })
        child1 = storage.insert_cluster({
            "name": "child1", "level": 1, "summary": "sub1",
            "parent_cluster_id": parent, "heat": 0.9,
        })
        child2 = storage.insert_cluster({
            "name": "child2", "level": 1, "summary": "sub2",
            "parent_cluster_id": parent, "heat": 0.5,
        })
        result = storage.get_child_clusters(parent)
        assert len(result) == 2
        assert result[0]["heat"] >= result[1]["heat"]  # ordered by heat DESC

    def test_get_cluster_dominant_directory(self, storage):
        cid = storage.insert_cluster({"name": "c1", "level": 1, "summary": "s"})
        _insert_memory(storage, "m1", directory="/project/a", cluster_id=cid)
        _insert_memory(storage, "m2", directory="/project/a", cluster_id=cid)
        _insert_memory(storage, "m3", directory="/project/b", cluster_id=cid)
        result = storage.get_cluster_dominant_directory(cid)
        assert result == "/project/a"

    def test_get_cluster_dominant_directory_empty(self, storage):
        assert storage.get_cluster_dominant_directory(99999) is None

    def test_get_cluster_member_ids(self, storage):
        cid = storage.insert_cluster({"name": "c1", "level": 1, "summary": "s"})
        m1 = _insert_memory(storage, "in", cluster_id=cid)
        m2 = _insert_memory(storage, "out")
        result = storage.get_cluster_member_ids(cid)
        assert m1 in result
        assert m2 not in result


# ═══════════════════════════════════════════════════════════════════════
# 1h. Action log, metadata, directory, rule, seed, astrocyte queries
# ═══════════════════════════════════════════════════════════════════════

class TestGetUnprocessedActionLog:
    def test_returns_unprocessed(self, storage):
        storage.execute_write(
            "INSERT INTO action_log (tool_name, tool_input_summary, directory, "
            "timestamp, processed) VALUES (?, ?, ?, ?, ?)",
            ("remember", "stored a memory", "/test", _now_iso(), 0),
        )
        result = storage.get_unprocessed_action_log()
        assert len(result) == 1
        assert result[0]["tool_name"] == "remember"

    def test_excludes_processed(self, storage):
        storage.execute_write(
            "INSERT INTO action_log (tool_name, tool_input_summary, directory, "
            "timestamp, processed) VALUES (?, ?, ?, ?, ?)",
            ("remember", "old", "/test", _now_iso(), 1),
        )
        result = storage.get_unprocessed_action_log()
        assert len(result) == 0

    def test_respects_limit(self, storage):
        for i in range(10):
            storage.execute_write(
                "INSERT INTO action_log (tool_name, tool_input_summary, directory, "
                "timestamp, processed) VALUES (?, ?, ?, ?, ?)",
                (f"tool_{i}", f"summary_{i}", "/test", _now_iso(), 0),
            )
        result = storage.get_unprocessed_action_log(limit=3)
        assert len(result) == 3


class TestMetadataValue:
    def test_get_existing(self, storage):
        storage.execute_write(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
            ("test_key", "test_value"),
        )
        assert storage.get_metadata_value("test_key") == "test_value"

    def test_get_missing(self, storage):
        assert storage.get_metadata_value("nonexistent") is None

    def test_set_and_get(self, storage):
        storage.set_metadata_value("new_key", "new_value")
        assert storage.get_metadata_value("new_key") == "new_value"

    def test_set_overwrites(self, storage):
        storage.set_metadata_value("key", "v1")
        storage.set_metadata_value("key", "v2")
        assert storage.get_metadata_value("key") == "v2"


class TestGetActiveDirectories:
    def test_returns_distinct_directories(self, storage):
        _insert_memory(storage, "m1", directory="/project/a")
        _insert_memory(storage, "m2", directory="/project/a")
        _insert_memory(storage, "m3", directory="/project/b")
        result = storage.get_active_directories()
        assert "/project/a" in result
        assert "/project/b" in result
        assert len(result) == 2

    def test_respects_min_heat(self, storage):
        _insert_memory(storage, "hot", directory="/hot", heat=0.9)
        _insert_memory(storage, "cold", directory="/cold", heat=0.0)
        result = storage.get_active_directories(min_heat=0.5)
        assert "/hot" in result
        assert "/cold" not in result

    def test_uses_gte_not_gt(self, storage):
        """get_active_directories uses >= (not >) to match narrative.py semantics."""
        _insert_memory(storage, "boundary", directory="/boundary", heat=0.5)
        result = storage.get_active_directories(min_heat=0.5)
        assert "/boundary" in result


class TestRuleQueries:
    def test_rule_exists(self, storage):
        rid = storage.insert_rule({
            "rule_type": "filter", "scope": "global",
            "condition": "heat < 0.1", "action": "suppress",
        })
        assert storage.rule_exists(rid)

    def test_rule_not_exists(self, storage):
        assert not storage.rule_exists(99999)

    def test_get_all_rules_sorted(self, storage):
        storage.insert_rule({
            "rule_type": "filter", "scope": "directory",
            "condition": "heat < 0.1", "action": "suppress",
            "priority": 5,
        })
        storage.insert_rule({
            "rule_type": "boost", "scope": "global",
            "condition": "importance > 0.8", "action": "boost",
            "priority": 10,
        })
        result = storage.get_all_rules_sorted()
        assert len(result) == 2
        # Should be ordered by scope, then priority DESC

    def test_get_rules_by_scope_type(self, storage):
        storage.insert_rule({
            "rule_type": "filter", "scope": "directory",
            "scope_value": "/project/a",
            "condition": "heat < 0.1", "action": "suppress",
        })
        storage.insert_rule({
            "rule_type": "filter", "scope": "directory",
            "scope_value": "/project/b",
            "condition": "heat < 0.2", "action": "suppress",
        })
        storage.insert_rule({
            "rule_type": "filter", "scope": "file",
            "condition": "heat < 0.3", "action": "suppress",
        })
        result = storage.get_rules_by_scope_type("directory")
        assert len(result) == 2  # both directory rules, no file rules


class TestGetMemoryIdsByTag:
    def test_finds_tagged_memories(self, storage):
        mid = _insert_memory(storage, "seeded", tags=["_seed", "test"])
        result = storage.get_memory_ids_by_tag("_seed")
        assert mid in result

    def test_no_false_positives(self, storage):
        _insert_memory(storage, "not seeded", tags=["test"])
        result = storage.get_memory_ids_by_tag("_seed")
        assert len(result) == 0


class TestGetAstrocyteProcess:
    def test_returns_process(self, storage):
        pid = storage.insert_astrocyte_process({
            "name": "code-patterns", "domain": "code-patterns",
            "specialization": "{}", "memory_ids": [], "entity_ids": [],
        })
        result = storage.get_astrocyte_process(pid)
        assert result is not None
        assert result["name"] == "code-patterns"

    def test_returns_none_not_found(self, storage):
        assert storage.get_astrocyte_process(99999) is None


# ═══════════════════════════════════════════════════════════════════════
# 1i. Write connection (write_queue support)
# ═══════════════════════════════════════════════════════════════════════

class TestGetWriteConnection:
    def test_returns_connection(self, storage):
        conn = storage.get_write_connection()
        assert conn is not None
        # Should be able to execute a query
        row = conn.execute("SELECT 1").fetchone()
        assert row[0] == 1

    def test_returns_same_connection_as_internal(self, storage):
        """The write connection should be the same thread-local connection."""
        conn = storage.get_write_connection()
        # Insert via connection, read via storage — should see the data
        conn.execute(
            "INSERT INTO metadata (key, value) VALUES (?, ?)",
            ("write_conn_test", "hello"),
        )
        conn.commit()
        assert storage.get_metadata_value("write_conn_test") == "hello"
