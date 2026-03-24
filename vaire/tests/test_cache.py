"""
Tests for Phase 2: MemoryCache (vaire/cache.py).

Covers:
  - CachedMemory dataclass construction
  - warmup() loads memories, entities, rules from a real (in-memory) DB
  - _build_embedding_matrix() builds correct (N, D) float32 matrix
  - vector_search() returns correct top-k results
  - vector_search() with project_dir filter
  - vector_search() on empty cache returns []
  - vector_search() with zero-norm query returns []
  - invalidate(MEMORY_UPSERT) adds + updates memory, marks tier-2 dirty
  - invalidate(MEMORY_DELETE) removes memory from all indexes
  - invalidate(HEAT_UPDATE) updates only heat, no tier-2 rebuild
  - invalidate(ENTITY_UPSERT) updates entity index
  - invalidate(RULE_UPSERT) replaces rules list
  - content_hash_exists() deduplication
  - get_memories_for_project() project filter
  - StorageEngine.get_all_memories_for_cache() excludes stale rows
  - StorageEngine.get_active_rules() returns priority-ordered active rules
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from vaire.cache import (
    CachedMemory,
    InvalidationEvent,
    MemoryCache,
    _sha256,
)
from vaire.storage import StorageEngine


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_db(tmp_path):
    """Return a real StorageEngine backed by a temporary SQLite DB."""
    db_path = tmp_path / "test.db"
    engine = StorageEngine(str(db_path), embedding_dim=4)
    yield engine
    engine.close()


def _make_embedding(values: list[float]) -> bytes:
    """Create a 4-float embedding blob for test fixtures."""
    return np.array(values, dtype=np.float32).tobytes()


def _insert_memory(engine: StorageEngine, content: str, project_dir: str = "/proj",
                   heat: float = 1.0, embedding_values: list[float] | None = None,
                   is_stale: int = 0) -> int:
    """Insert a memory row directly and return its id."""
    emb_blob = _make_embedding(embedding_values) if embedding_values else None
    now = "2026-01-01T00:00:00"
    cur = engine._conn.execute(
        "INSERT INTO memories(content, embedding, tags, directory_context, "
        "created_at, last_accessed, heat, is_stale) VALUES (?,?,?,?,?,?,?,?)",
        (content, emb_blob, "[]", project_dir, now, now, heat, is_stale),
    )
    engine._conn.commit()
    return cur.lastrowid


def _insert_entity(engine: StorageEngine, name: str, entity_type: str = "concept") -> int:
    now = "2026-01-01T00:00:00"
    cur = engine._conn.execute(
        "INSERT INTO entities(name, type, created_at, last_accessed, heat) "
        "VALUES (?,?,?,?,?)",
        (name, entity_type, now, now, 1.0),
    )
    engine._conn.commit()
    return cur.lastrowid


def _insert_rule(engine: StorageEngine, rule_type: str = "protect",
                 priority: int = 0, is_active: int = 1) -> int:
    now = "2026-01-01T00:00:00"
    cur = engine._conn.execute(
        "INSERT INTO memory_rules(rule_type, scope, scope_value, condition, "
        "action, priority, created_at, is_active) VALUES (?,?,?,?,?,?,?,?)",
        (rule_type, "global", None, "always", "protect", priority, now, is_active),
    )
    engine._conn.commit()
    return cur.lastrowid


@pytest.fixture
def cache(tmp_db):
    return MemoryCache(tmp_db)


# ── Storage method tests ───────────────────────────────────────────────────────

class TestStorageGetAllMemoriesForCache:
    """StorageEngine.get_all_memories_for_cache() must exclude stale rows."""

    def test_returns_non_stale_memories(self, tmp_db):
        _insert_memory(tmp_db, "alpha", is_stale=0)
        _insert_memory(tmp_db, "beta", is_stale=0)
        rows = tmp_db.get_all_memories_for_cache()
        contents = {r["content"] for r in rows}
        assert "alpha" in contents
        assert "beta" in contents

    def test_excludes_stale_rows(self, tmp_db):
        _insert_memory(tmp_db, "fresh", is_stale=0)
        _insert_memory(tmp_db, "stale", is_stale=1)
        rows = tmp_db.get_all_memories_for_cache()
        contents = {r["content"] for r in rows}
        assert "fresh" in contents
        assert "stale" not in contents

    def test_returns_required_fields(self, tmp_db):
        _insert_memory(tmp_db, "field-check", embedding_values=[1.0, 0.0, 0.0, 0.0])
        rows = tmp_db.get_all_memories_for_cache()
        assert len(rows) == 1
        row = rows[0]
        for field in ("id", "content", "heat", "embedding", "tags",
                      "directory_context", "created_at", "last_accessed"):
            assert field in row, f"Missing field: {field}"

    def test_empty_db_returns_empty_list(self, tmp_db):
        assert tmp_db.get_all_memories_for_cache() == []


class TestStorageGetActiveRules:
    """StorageEngine.get_active_rules() returns active rules priority DESC."""

    def test_returns_only_active_rules(self, tmp_db):
        _insert_rule(tmp_db, "protect", priority=5, is_active=1)
        _insert_rule(tmp_db, "archive", priority=10, is_active=0)
        rules = tmp_db.get_active_rules()
        assert len(rules) == 1
        assert rules[0]["rule_type"] == "protect"

    def test_sorted_priority_desc(self, tmp_db):
        _insert_rule(tmp_db, "low", priority=1)
        _insert_rule(tmp_db, "high", priority=100)
        _insert_rule(tmp_db, "mid", priority=50)
        rules = tmp_db.get_active_rules()
        priorities = [r["priority"] for r in rules]
        assert priorities == sorted(priorities, reverse=True)

    def test_empty_returns_empty(self, tmp_db):
        assert tmp_db.get_active_rules() == []


# ── CachedMemory ───────────────────────────────────────────────────────────────

class TestCachedMemory:
    def test_construction(self):
        emb = np.zeros(4, dtype=np.float32)
        cm = CachedMemory(
            id=1, content="hello", heat=0.8, embedding=emb,
            tags=["a"], project_dir="/p", created_at="now", last_accessed="now",
        )
        assert cm.id == 1
        assert cm.content == "hello"
        assert cm.heat == pytest.approx(0.8)
        assert cm.tags == ["a"]

    def test_embedding_can_be_none(self):
        cm = CachedMemory(
            id=2, content="x", heat=1.0, embedding=None,
            tags=[], project_dir="/q", created_at="", last_accessed="",
        )
        assert cm.embedding is None


# ── Warmup ─────────────────────────────────────────────────────────────────────

class TestWarmup:
    def test_warmup_loads_memories(self, cache, tmp_db):
        _insert_memory(tmp_db, "warm memory", embedding_values=[1.0, 0.0, 0.0, 0.0])
        import asyncio
        asyncio.run(cache.warmup())
        assert len(cache._memories) == 1

    def test_warmup_excludes_stale(self, cache, tmp_db):
        _insert_memory(tmp_db, "live", is_stale=0)
        _insert_memory(tmp_db, "dead", is_stale=1)
        import asyncio
        asyncio.run(cache.warmup())
        contents = {cm.content for cm in cache._memories.values()}
        assert "live" in contents
        assert "dead" not in contents

    def test_warmup_loads_entities(self, cache, tmp_db):
        _insert_entity(tmp_db, "Python")
        import asyncio
        asyncio.run(cache.warmup())
        assert "Python" in cache._entities

    def test_warmup_loads_rules(self, cache, tmp_db):
        _insert_rule(tmp_db, "protect", priority=10)
        import asyncio
        asyncio.run(cache.warmup())
        assert len(cache._rules) == 1

    def test_warmup_decodes_embedding(self, cache, tmp_db):
        _insert_memory(tmp_db, "embed-test", embedding_values=[1.0, 2.0, 3.0, 4.0])
        import asyncio
        asyncio.run(cache.warmup())
        (cm,) = cache._memories.values()
        assert cm.embedding is not None
        assert cm.embedding.shape == (4,)
        np.testing.assert_allclose(cm.embedding, [1.0, 2.0, 3.0, 4.0], rtol=1e-5)

    def test_warmup_builds_content_index(self, cache, tmp_db):
        _insert_memory(tmp_db, "hello world")
        import asyncio
        asyncio.run(cache.warmup())
        assert cache.content_hash_exists("hello world")
        assert not cache.content_hash_exists("not stored")

    def test_warmup_builds_project_index(self, cache, tmp_db):
        _insert_memory(tmp_db, "proj mem", project_dir="/myproj")
        import asyncio
        asyncio.run(cache.warmup())
        assert "/myproj" in cache._project_index


# ── Embedding matrix ───────────────────────────────────────────────────────────

class TestBuildEmbeddingMatrix:
    def test_matrix_shape(self, cache, tmp_db):
        for i in range(3):
            _insert_memory(tmp_db, f"mem{i}", embedding_values=[float(i), 0.0, 0.0, 0.0])
        import asyncio
        asyncio.run(cache.warmup())
        cache._build_embedding_matrix()
        assert cache._embedding_matrix is not None
        assert cache._embedding_matrix.shape == (3, 4)

    def test_matrix_is_l2_normalised(self, cache, tmp_db):
        _insert_memory(tmp_db, "vec", embedding_values=[3.0, 4.0, 0.0, 0.0])
        import asyncio
        asyncio.run(cache.warmup())
        cache._build_embedding_matrix()
        norms = np.linalg.norm(cache._embedding_matrix, axis=1)
        np.testing.assert_allclose(norms, [1.0], atol=1e-6)

    def test_no_embeddings_gives_none_matrix(self, cache, tmp_db):
        _insert_memory(tmp_db, "no embed", embedding_values=None)
        import asyncio
        asyncio.run(cache.warmup())
        cache._build_embedding_matrix()
        assert cache._embedding_matrix is None

    def test_dirty_flag_cleared_after_build(self, cache, tmp_db):
        import asyncio
        asyncio.run(cache.warmup())
        cache._build_embedding_matrix()
        assert cache._dirty_tier2 is False


# ── Vector search ──────────────────────────────────────────────────────────────

class TestVectorSearch:
    def _make_cache_with_vecs(self, cache, tmp_db, vecs: list[list[float]],
                               project_dirs: list[str] | None = None):
        for i, v in enumerate(vecs):
            proj = (project_dirs[i] if project_dirs else "/proj")
            _insert_memory(tmp_db, f"mem{i}", project_dir=proj, embedding_values=v)
        import asyncio
        asyncio.run(cache.warmup())

    def test_returns_empty_on_empty_cache(self, cache):
        q = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        assert cache.vector_search(q, top_k=5) == []

    def test_closest_vector_ranked_first(self, cache, tmp_db):
        self._make_cache_with_vecs(cache, tmp_db, [
            [1.0, 0.0, 0.0, 0.0],   # identical to query
            [0.0, 1.0, 0.0, 0.0],   # orthogonal
        ])
        q = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        results = cache.vector_search(q, top_k=2)
        assert len(results) == 2
        top_id, top_score = results[0]
        assert top_score == pytest.approx(1.0, abs=1e-5)

    def test_top_k_limits_results(self, cache, tmp_db):
        for i in range(5):
            v = [float(i == j) for j in range(4)]
            _insert_memory(tmp_db, f"m{i}", embedding_values=v)
        import asyncio
        asyncio.run(cache.warmup())
        q = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        results = cache.vector_search(q, top_k=2)
        assert len(results) <= 2

    def test_project_filter_restricts_results(self, cache, tmp_db):
        self._make_cache_with_vecs(cache, tmp_db,
            [[1.0, 0.0, 0.0, 0.0], [0.9, 0.1, 0.0, 0.0]],
            ["/projA", "/projB"],
        )
        q = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        results = cache.vector_search(q, top_k=10, project_dir="/projA")
        result_ids = [r[0] for r in results]
        # Only memories in /projA should appear
        for mem_id in result_ids:
            assert cache._memories[mem_id].project_dir == "/projA"

    def test_project_filter_empty_project_returns_empty(self, cache, tmp_db):
        _insert_memory(tmp_db, "mem", project_dir="/projA",
                       embedding_values=[1.0, 0.0, 0.0, 0.0])
        import asyncio
        asyncio.run(cache.warmup())
        q = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        results = cache.vector_search(q, top_k=5, project_dir="/projB")
        assert results == []

    def test_zero_norm_query_returns_empty(self, cache, tmp_db):
        _insert_memory(tmp_db, "vec", embedding_values=[1.0, 0.0, 0.0, 0.0])
        import asyncio
        asyncio.run(cache.warmup())
        q = np.zeros(4, dtype=np.float32)
        assert cache.vector_search(q, top_k=5) == []

    def test_scores_are_floats(self, cache, tmp_db):
        _insert_memory(tmp_db, "f", embedding_values=[1.0, 0.0, 0.0, 0.0])
        import asyncio
        asyncio.run(cache.warmup())
        q = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        results = cache.vector_search(q, top_k=1)
        assert len(results) == 1
        assert isinstance(results[0][0], int)
        assert isinstance(results[0][1], float)

    def test_results_sorted_desc(self, cache, tmp_db):
        for i, v in enumerate([[1.0, 0.0, 0.0, 0.0], [0.5, 0.5, 0.5, 0.5],
                                 [0.0, 0.0, 0.0, 1.0]]):
            _insert_memory(tmp_db, f"m{i}", embedding_values=v)
        import asyncio
        asyncio.run(cache.warmup())
        q = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        results = cache.vector_search(q, top_k=3)
        scores = [s for _, s in results]
        assert scores == sorted(scores, reverse=True)


# ── Invalidation ───────────────────────────────────────────────────────────────

class TestInvalidation:
    def _warmed(self, cache, tmp_db):
        import asyncio
        asyncio.run(cache.warmup())
        return cache

    def test_memory_upsert_adds_new(self, cache, tmp_db):
        self._warmed(cache, tmp_db)
        cache.invalidate(
            InvalidationEvent.MEMORY_UPSERT,
            id=99, content="new content", heat=1.0,
            embedding=None, tags=[], project_dir="/p",
            created_at="", last_accessed="",
        )
        assert 99 in cache._memories
        assert cache._memories[99].content == "new content"

    def test_memory_upsert_updates_existing(self, cache, tmp_db):
        mid = _insert_memory(tmp_db, "original")
        self._warmed(cache, tmp_db)
        cache.invalidate(
            InvalidationEvent.MEMORY_UPSERT,
            id=mid, content="updated", heat=0.5,
            embedding=None, tags=[], project_dir="/proj",
            created_at="", last_accessed="",
        )
        assert cache._memories[mid].content == "updated"
        assert cache._memories[mid].heat == pytest.approx(0.5)

    def test_memory_upsert_updates_content_index(self, cache, tmp_db):
        self._warmed(cache, tmp_db)
        cache.invalidate(
            InvalidationEvent.MEMORY_UPSERT,
            id=42, content="track this", heat=1.0,
            embedding=None, tags=[], project_dir="/p",
            created_at="", last_accessed="",
        )
        assert cache.content_hash_exists("track this")

    def test_memory_upsert_marks_dirty(self, cache, tmp_db):
        self._warmed(cache, tmp_db)
        cache._dirty_tier2 = False  # force clean
        cache.invalidate(
            InvalidationEvent.MEMORY_UPSERT,
            id=7, content="x", heat=1.0, embedding=None,
            tags=[], project_dir="/p", created_at="", last_accessed="",
        )
        assert cache._dirty_tier2 is True

    def test_memory_delete_removes(self, cache, tmp_db):
        mid = _insert_memory(tmp_db, "to delete")
        self._warmed(cache, tmp_db)
        cache.invalidate(InvalidationEvent.MEMORY_DELETE, id=mid)
        assert mid not in cache._memories
        assert not cache.content_hash_exists("to delete")

    def test_memory_delete_marks_dirty(self, cache, tmp_db):
        mid = _insert_memory(tmp_db, "gone")
        self._warmed(cache, tmp_db)
        cache._dirty_tier2 = False
        cache.invalidate(InvalidationEvent.MEMORY_DELETE, id=mid)
        assert cache._dirty_tier2 is True

    def test_heat_update_does_not_mark_dirty(self, cache, tmp_db):
        mid = _insert_memory(tmp_db, "heat-only")
        self._warmed(cache, tmp_db)
        cache._dirty_tier2 = False
        cache.invalidate(InvalidationEvent.HEAT_UPDATE, id=mid, new_heat=0.3)
        assert cache._dirty_tier2 is False
        assert cache._memories[mid].heat == pytest.approx(0.3)

    def test_heat_update_noop_on_missing(self, cache, tmp_db):
        self._warmed(cache, tmp_db)
        # Should not raise
        cache.invalidate(InvalidationEvent.HEAT_UPDATE, id=9999, new_heat=0.5)

    def test_entity_upsert_updates_entity_index(self, cache, tmp_db):
        self._warmed(cache, tmp_db)
        cache.invalidate(
            InvalidationEvent.ENTITY_UPSERT,
            name="Django", row={"name": "Django", "type": "framework"},
        )
        assert "Django" in cache._entities
        assert cache._entities["Django"]["type"] == "framework"

    def test_rule_upsert_replaces_rules(self, cache, tmp_db):
        _insert_rule(tmp_db, "old-rule")
        self._warmed(cache, tmp_db)
        new_rules = [{"id": 99, "rule_type": "new-rule", "priority": 5}]
        cache.invalidate(InvalidationEvent.RULE_UPSERT, rules=new_rules)
        assert len(cache._rules) == 1
        assert cache._rules[0]["rule_type"] == "new-rule"

    def test_relationship_upsert_is_noop(self, cache, tmp_db):
        self._warmed(cache, tmp_db)
        # Should not raise and should not change any index
        before = dict(cache._memories)
        cache.invalidate(InvalidationEvent.RELATIONSHIP_UPSERT,
                         source="A", target="B", rel_type="relates")
        assert cache._memories == before


# ── Read helpers ───────────────────────────────────────────────────────────────

class TestReadHelpers:
    def test_get_memory(self, cache, tmp_db):
        mid = _insert_memory(tmp_db, "specific")
        import asyncio
        asyncio.run(cache.warmup())
        cm = cache.get_memory(mid)
        assert cm is not None
        assert cm.content == "specific"

    def test_get_memory_missing(self, cache):
        assert cache.get_memory(9999) is None

    def test_get_memories_for_project(self, cache, tmp_db):
        _insert_memory(tmp_db, "in proj", project_dir="/myp")
        _insert_memory(tmp_db, "other", project_dir="/other")
        import asyncio
        asyncio.run(cache.warmup())
        mems = cache.get_memories_for_project("/myp")
        assert len(mems) == 1
        assert mems[0].content == "in proj"

    def test_content_hash_exists(self, cache, tmp_db):
        _insert_memory(tmp_db, "unique content")
        import asyncio
        asyncio.run(cache.warmup())
        assert cache.content_hash_exists("unique content")
        assert not cache.content_hash_exists("not there")

    def test_memory_count(self, cache, tmp_db):
        for i in range(4):
            _insert_memory(tmp_db, f"mem{i}")
        import asyncio
        asyncio.run(cache.warmup())
        assert cache.memory_count == 4

    def test_entity_count(self, cache, tmp_db):
        _insert_entity(tmp_db, "Alice")
        _insert_entity(tmp_db, "Bob")
        import asyncio
        asyncio.run(cache.warmup())
        assert cache.entity_count == 2

    def test_embedded_count(self, cache, tmp_db):
        _insert_memory(tmp_db, "embedded", embedding_values=[1.0, 0.0, 0.0, 0.0])
        _insert_memory(tmp_db, "no embedding")
        import asyncio
        asyncio.run(cache.warmup())
        assert cache.embedded_count == 1


# ── SHA256 helper ──────────────────────────────────────────────────────────────

class TestSha256Helper:
    def test_deterministic(self):
        assert _sha256("hello") == _sha256("hello")

    def test_different_inputs_differ(self):
        assert _sha256("a") != _sha256("b")

    def test_returns_string(self):
        assert isinstance(_sha256("x"), str)
