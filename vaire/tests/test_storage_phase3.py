"""
Tests for Phase 3: StorageEngine Group A / B / C method additions.

Group A — recall hot path:
  get_memories_by_ids, get_hot_memories_for_project, get_fts_matches,
  get_relationships_for_entities, get_entity_relationships_graph

Group B — write path [COMMITS]:
  upsert_memory (insert + update paths), update_memory_access,
  update_memory_tags, upsert_entity (insert + reinforce),
  upsert_relationship (insert + reinforce), get_memory_by_id,
  begin_transaction/commit/rollback, batch_update_heat

Group C — background/consolidation:
  get_memories_for_decay, get_duplicate_candidates,
  get_orphaned_entities, get_stale_memories_by_age,
  get_consolidation_candidates, mark_memory_archived,
  get_episodes_for_memory, upsert_crdt_entry
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

import numpy as np
import pytest

from vaire.storage import StorageEngine


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    engine = StorageEngine(str(tmp_path / "test.db"), embedding_dim=4)
    yield engine
    engine.close()


def _emb(values: list[float]) -> bytes:
    return np.array(values, dtype=np.float32).tobytes()


def _now_minus(days: int = 0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.isoformat()


def _insert_mem(db: StorageEngine, content: str = "content",
                project_dir: str = "/proj", heat: float = 1.0,
                embedding: bytes | None = None, is_stale: int = 0,
                last_accessed: str | None = None) -> int:
    now = _now_minus(0)
    cur = db._test_conn.execute(
        "INSERT INTO memories(content, embedding, tags, directory_context, "
        "created_at, last_accessed, heat, is_stale) VALUES (?,?,?,?,?,?,?,?)",
        (content, embedding, "[]", project_dir, now,
         last_accessed or now, heat, is_stale),
    )
    mid = cur.lastrowid
    # Explicit FTS sync (triggers removed in Phase 2)
    db._test_conn.execute(
        "INSERT INTO memories_fts(rowid, content) VALUES (?, ?)",
        (mid, content),
    )
    db._test_conn.commit()
    return mid


def _insert_entity(db: StorageEngine, name: str, entity_type: str = "concept") -> int:
    now = _now_minus(0)
    cur = db._test_conn.execute(
        "INSERT INTO entities(name, type, created_at, last_accessed, heat, archived) "
        "VALUES (?,?,?,?,?,0)",
        (name, entity_type, now, now, 1.0),
    )
    db._test_conn.commit()
    return cur.lastrowid


def _insert_rel(db: StorageEngine, src_id: int, tgt_id: int,
                rel_type: str = "relates") -> int:
    now = _now_minus(0)
    cur = db._test_conn.execute(
        "INSERT INTO relationships(source_entity_id, target_entity_id, "
        "relationship_type, weight, created_at, last_reinforced) VALUES (?,?,?,1,?,?)",
        (src_id, tgt_id, rel_type, now, now),
    )
    db._test_conn.commit()
    return cur.lastrowid


# ══════════════════════════════════════════════════════════════════════════════
# Group A — recall hot path
# ══════════════════════════════════════════════════════════════════════════════

class TestGetMemoriesByIds:
    def test_returns_correct_memories(self, db):
        a = _insert_mem(db, "alpha")
        b = _insert_mem(db, "beta")
        _insert_mem(db, "gamma")
        rows = db.get_memories_by_ids([a, b])
        contents = {r["content"] for r in rows}
        assert contents == {"alpha", "beta"}

    def test_empty_ids_returns_empty(self, db):
        _insert_mem(db, "x")
        assert db.get_memories_by_ids([]) == []

    def test_nonexistent_ids_returns_empty(self, db):
        assert db.get_memories_by_ids([9999, 8888]) == []


class TestGetHotMemoriesForProject:
    def test_returns_only_project_memories(self, db):
        _insert_mem(db, "in proj", project_dir="/proj", heat=0.8)
        _insert_mem(db, "other",   project_dir="/other", heat=0.8)
        rows = db.get_hot_memories_for_project("/proj", min_heat=0.0)
        assert all(r["directory_context"] == "/proj" for r in rows)
        assert len(rows) == 1

    def test_excludes_below_min_heat(self, db):
        _insert_mem(db, "hot",  project_dir="/p", heat=0.9)
        _insert_mem(db, "cold", project_dir="/p", heat=0.1)
        rows = db.get_hot_memories_for_project("/p", min_heat=0.5)
        assert len(rows) == 1
        assert rows[0]["content"] == "hot"

    def test_excludes_stale(self, db):
        _insert_mem(db, "live",  project_dir="/p", heat=1.0, is_stale=0)
        _insert_mem(db, "stale", project_dir="/p", heat=1.0, is_stale=1)
        rows = db.get_hot_memories_for_project("/p", min_heat=0.0)
        assert len(rows) == 1
        assert rows[0]["content"] == "live"

    def test_sorted_heat_desc(self, db):
        _insert_mem(db, "low",  project_dir="/p", heat=0.2)
        _insert_mem(db, "high", project_dir="/p", heat=0.9)
        rows = db.get_hot_memories_for_project("/p", min_heat=0.0)
        heats = [r["heat"] for r in rows]
        assert heats == sorted(heats, reverse=True)


class TestGetFtsMatches:
    def test_returns_matches(self, db):
        mid = _insert_mem(db, "asyncio event loop python")
        results = db.get_fts_matches("asyncio")
        ids = [r[0] for r in results]
        assert mid in ids

    def test_returns_tuples_of_int_float(self, db):
        _insert_mem(db, "python programming language")
        results = db.get_fts_matches("python")
        for r in results:
            assert isinstance(r[0], int)
            assert isinstance(r[1], float)

    def test_no_match_returns_empty(self, db):
        _insert_mem(db, "totally unrelated content")
        results = db.get_fts_matches("zzz_no_match_xyz")
        assert results == []


class TestGetRelationshipsForEntities:
    def test_returns_relationships(self, db):
        a = _insert_entity(db, "Python")
        b = _insert_entity(db, "Django")
        _insert_rel(db, a, b, "uses")
        rows = db.get_relationships_for_entities(["Python"])
        assert len(rows) == 1

    def test_matches_source_or_target(self, db):
        a = _insert_entity(db, "React")
        b = _insert_entity(db, "JavaScript")
        _insert_rel(db, a, b, "written_in")
        assert len(db.get_relationships_for_entities(["JavaScript"])) == 1

    def test_empty_names_returns_empty(self, db):
        assert db.get_relationships_for_entities([]) == []


class TestGetEntityRelationshipsGraph:
    def test_returns_all_relationships(self, db):
        a = _insert_entity(db, "A")
        b = _insert_entity(db, "B")
        _insert_rel(db, a, b, "linked")
        graph = db.get_entity_relationships_graph()
        assert len(graph) == 1
        assert "source_name" in graph[0]
        assert "target_name" in graph[0]
        assert graph[0]["source_name"] == "A"
        assert graph[0]["target_name"] == "B"

    def test_empty_returns_empty(self, db):
        assert db.get_entity_relationships_graph() == []


# ══════════════════════════════════════════════════════════════════════════════
# Group B — write path
# ══════════════════════════════════════════════════════════════════════════════

class TestUpsertMemory:
    def test_insert_new_returns_id(self, db):
        mid = db.upsert_memory("hello", None, 1.0, "/p", [], "agent1")
        assert isinstance(mid, int)
        assert mid > 0

    def test_insert_stores_content(self, db):
        mid = db.upsert_memory("store me", None, 0.8, "/p", ["t"], "a1")
        row = db.get_memory_by_id(mid)
        assert row["content"] == "store me"
        assert row["heat"] == pytest.approx(0.8)

    def test_update_existing(self, db):
        mid = db.upsert_memory("original", None, 1.0, "/p", [], "a1")
        db.upsert_memory("updated", None, 0.5, "/p", [], "a2", memory_id=mid)
        row = db.get_memory_by_id(mid)
        assert row["content"] == "updated"
        assert row["heat"] == pytest.approx(0.5)

    def test_update_nonexistent_id_inserts_new(self, db):
        mid = db.upsert_memory("new", None, 1.0, "/p", [], "a1", memory_id=9999)
        assert mid != 9999  # inserted as new row

    def test_stores_embedding(self, db):
        emb = _emb([1.0, 2.0, 3.0, 4.0])
        mid = db.upsert_memory("with emb", emb, 1.0, "/p", [], "a1")
        row = db.get_memory_by_id(mid)
        assert row["embedding"] is not None


class TestUpdateMemoryAccess:
    def test_updates_heat_and_accessed_at(self, db):
        mid = _insert_mem(db, "access me", heat=1.0)
        new_time = _now_minus(0)
        db.update_memory_access(mid, 0.7, new_time)
        row = db.get_memory_by_id(mid)
        assert row["heat"] == pytest.approx(0.7)
        assert row["last_accessed"] == new_time


class TestUpdateMemoryTags:
    def test_updates_tags(self, db):
        mid = _insert_mem(db, "tag me")
        db.update_memory_tags(mid, ["python", "asyncio"], "agent1")
        row = db.get_memory_by_id(mid)
        tags = row["tags"] if isinstance(row["tags"], list) else json.loads(row["tags"])
        assert "python" in tags
        assert "asyncio" in tags


class TestUpsertEntity:
    def test_insert_new_entity(self, db):
        eid = db.upsert_entity("Rust", "language", "a1")
        assert isinstance(eid, int)
        entity = db.get_entity_by_name("Rust")
        assert entity is not None
        assert entity["type"] == "language"

    def test_reinforce_existing(self, db):
        eid1 = db.upsert_entity("Go", "language", "a1", heat=0.5)
        eid2 = db.upsert_entity("Go", "language", "a1", heat=0.9)
        assert eid1 == eid2  # same entity

    def test_unarchives_existing(self, db):
        eid = db.upsert_entity("Java", "language", "a1")
        db._test_conn.execute("UPDATE entities SET archived=1 WHERE id=?", (eid,))
        db._test_conn.commit()
        db.upsert_entity("Java", "language", "a1")
        entity = db.get_entity_by_name("Java")
        assert entity["archived"] == 0


class TestUpsertRelationship:
    def test_inserts_new_relationship(self, db):
        rid = db.upsert_relationship("Python", "Django", "framework_of")
        assert isinstance(rid, int)
        assert rid > 0

    def test_creates_entities_if_missing(self, db):
        db.upsert_relationship("Rust", "WebAssembly", "compiles_to")
        assert db.get_entity_by_name("Rust") is not None
        assert db.get_entity_by_name("WebAssembly") is not None

    def test_reinforces_existing(self, db):
        rid1 = db.upsert_relationship("A", "B", "related")
        rid2 = db.upsert_relationship("A", "B", "related")
        assert rid1 == rid2

    def test_weight_increases_on_reinforce(self, db):
        rid = db.upsert_relationship("X", "Y", "linked")
        db.upsert_relationship("X", "Y", "linked")
        row = db._test_conn.execute(
            "SELECT weight FROM relationships WHERE id=?", (rid,)
        ).fetchone()
        assert row[0] >= 2.0


class TestGetMemoryById:
    def test_returns_memory(self, db):
        mid = _insert_mem(db, "specific")
        row = db.get_memory_by_id(mid)
        assert row is not None
        assert row["content"] == "specific"

    def test_returns_none_for_missing(self, db):
        assert db.get_memory_by_id(9999) is None


class TestTransactionMethods:
    def test_begin_commit(self, db):
        db.begin_transaction()
        db._test_conn.execute(
            "INSERT INTO memories(content, tags, directory_context, "
            "created_at, last_accessed, heat) VALUES (?,?,?,?,?,?)",
            ("tx mem", "[]", "/p", "2026-01-01", "2026-01-01", 1.0),
        )
        db.commit()
        rows = db._test_conn.execute(
            "SELECT * FROM memories WHERE content='tx mem'"
        ).fetchall()
        assert len(rows) == 1

    def test_begin_rollback(self, db):
        db.begin_transaction()
        db._test_conn.execute(
            "INSERT INTO memories(content, tags, directory_context, "
            "created_at, last_accessed, heat) VALUES (?,?,?,?,?,?)",
            ("rollback mem", "[]", "/p", "2026-01-01", "2026-01-01", 1.0),
        )
        db.rollback()
        rows = db._test_conn.execute(
            "SELECT * FROM memories WHERE content='rollback mem'"
        ).fetchall()
        assert len(rows) == 0


class TestBatchUpdateHeat:
    def test_updates_multiple_memories(self, db):
        a = _insert_mem(db, "a", heat=1.0)
        b = _insert_mem(db, "b", heat=1.0)
        db.batch_update_heat([(a, 0.3), (b, 0.7)])
        assert db.get_memory_by_id(a)["heat"] == pytest.approx(0.3)
        assert db.get_memory_by_id(b)["heat"] == pytest.approx(0.7)

    def test_empty_list_is_noop(self, db):
        db.batch_update_heat([])  # must not raise


# ══════════════════════════════════════════════════════════════════════════════
# Group C — background / consolidation
# ══════════════════════════════════════════════════════════════════════════════

class TestGetMemoriesForDecay:
    def test_returns_old_memories(self, db):
        old = _insert_mem(db, "old", heat=0.5,
                          last_accessed=_now_minus(30))
        new = _insert_mem(db, "new", heat=0.5,
                          last_accessed=_now_minus(0))
        cutoff = _now_minus(7)
        rows = db.get_memories_for_decay(cutoff, min_heat=0.0)
        ids = {r["id"] for r in rows}
        assert old in ids
        assert new not in ids

    def test_excludes_below_min_heat(self, db):
        _insert_mem(db, "low", heat=0.01, last_accessed=_now_minus(30))
        cutoff = _now_minus(7)
        rows = db.get_memories_for_decay(cutoff, min_heat=0.1)
        assert len(rows) == 0

    def test_excludes_stale(self, db):
        _insert_mem(db, "stale", heat=0.5,
                    last_accessed=_now_minus(30), is_stale=1)
        rows = db.get_memories_for_decay(_now_minus(7))
        assert len(rows) == 0


class TestGetDuplicateCandidates:
    def test_finds_near_duplicates(self, db):
        # Two almost-identical vectors
        e1 = _emb([1.0, 0.0, 0.0, 0.0])
        e2 = _emb([0.999, 0.001, 0.0, 0.0])
        e3 = _emb([0.0, 1.0, 0.0, 0.0])  # orthogonal — should not match
        a = _insert_mem(db, "alpha", embedding=e1)
        b = _insert_mem(db, "beta",  embedding=e2)
        _insert_mem(db, "gamma", embedding=e3)
        pairs = db.get_duplicate_candidates(threshold=0.99)
        pair_ids = {(p[0], p[1]) for p in pairs}
        assert (a, b) in pair_ids or (b, a) in pair_ids

    def test_returns_empty_if_fewer_than_two(self, db):
        _insert_mem(db, "lone", embedding=_emb([1.0, 0.0, 0.0, 0.0]))
        assert db.get_duplicate_candidates() == []

    def test_sorted_by_similarity_desc(self, db):
        e1 = _emb([1.0, 0.0, 0.0, 0.0])
        e2 = _emb([0.99, 0.01, 0.0, 0.0])
        e3 = _emb([0.95, 0.05, 0.0, 0.0])
        _insert_mem(db, "a", embedding=e1)
        _insert_mem(db, "b", embedding=e2)
        _insert_mem(db, "c", embedding=e3)
        pairs = db.get_duplicate_candidates(threshold=0.0)
        sims = [p[2] for p in pairs]
        assert sims == sorted(sims, reverse=True)


class TestGetOrphanedEntities:
    def test_finds_orphans(self, db):
        orphan = _insert_entity(db, "Orphan")
        connected_a = _insert_entity(db, "ConnA")
        connected_b = _insert_entity(db, "ConnB")
        _insert_rel(db, connected_a, connected_b)
        orphans = db.get_orphaned_entities()
        ids = {r["id"] for r in orphans}
        assert orphan in ids
        assert connected_a not in ids
        assert connected_b not in ids

    def test_excludes_archived(self, db):
        eid = _insert_entity(db, "Archived")
        db._test_conn.execute("UPDATE entities SET archived=1 WHERE id=?", (eid,))
        db._test_conn.commit()
        orphans = db.get_orphaned_entities()
        assert all(r["id"] != eid for r in orphans)


class TestGetStaleMemoriesByAge:
    def test_returns_old_low_heat_memories(self, db):
        old_cold = _insert_mem(db, "old cold", heat=0.1,
                               last_accessed=_now_minus(90))
        _insert_mem(db, "recent",  heat=0.1, last_accessed=_now_minus(0))
        _insert_mem(db, "old hot", heat=0.9, last_accessed=_now_minus(90))
        rows = db.get_stale_memories_by_age(days_since_access=30, max_heat=0.3)
        ids = {r["id"] for r in rows}
        assert old_cold in ids


class TestGetConsolidationCandidates:
    def test_returns_lowest_heat_first(self, db):
        _insert_mem(db, "hi", heat=0.9)
        _insert_mem(db, "lo", heat=0.1)
        rows = db.get_consolidation_candidates(limit=10)
        heats = [r["heat"] for r in rows]
        assert heats == sorted(heats)

    def test_respects_limit(self, db):
        for i in range(10):
            _insert_mem(db, f"m{i}", heat=float(i) / 10)
        rows = db.get_consolidation_candidates(limit=3)
        assert len(rows) == 3


class TestMarkMemoryArchived:
    def test_sets_is_stale(self, db):
        mid = _insert_mem(db, "to archive")
        db.mark_memory_archived(mid)
        row = db.get_memory_by_id(mid)
        assert row["is_stale"] == 1

    def test_archived_excluded_from_cache(self, db):
        mid = _insert_mem(db, "archive me")
        db.mark_memory_archived(mid)
        rows = db.get_all_memories_for_cache()
        assert all(r["id"] != mid for r in rows)


class TestGetEpisodesForMemory:
    def test_returns_sourcing_episode(self, db):
        ep_id = db._test_conn.execute(
            "INSERT INTO episodes(session_id, timestamp, directory, raw_content) "
            "VALUES (?,?,?,?)",
            ("sess1", "2026-01-01", "/proj", "raw content"),
        ).lastrowid
        db._test_conn.commit()
        mid = db._test_conn.execute(
            "INSERT INTO memories(content, tags, directory_context, "
            "created_at, last_accessed, heat, source_episode_id) "
            "VALUES (?,?,?,?,?,?,?)",
            ("sourced", "[]", "/proj", "2026-01-01", "2026-01-01", 1.0, ep_id),
        ).lastrowid
        db._test_conn.commit()
        eps = db.get_episodes_for_memory(mid)
        assert len(eps) == 1
        assert eps[0]["id"] == ep_id

    def test_returns_empty_for_no_source(self, db):
        mid = _insert_mem(db, "no episode")
        assert db.get_episodes_for_memory(mid) == []


class TestUpsertCrdtEntry:
    def test_inserts_entry(self, db):
        db.upsert_crdt_entry(1, "agent-1", "upsert", '{"agent-1": 1}', "2026-01-01T00:00:00")
        rows = db._test_conn.execute("SELECT * FROM crdt_entries").fetchall()
        assert len(rows) == 1

    def test_creates_table_lazily(self, db):
        # Table should not exist yet
        tables = db._test_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='crdt_entries'"
        ).fetchall()
        assert len(tables) == 0
        db.upsert_crdt_entry(1, "agent-1", "upsert", "{}", "2026-01-01T00:00:00")
        tables = db._test_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='crdt_entries'"
        ).fetchall()
        assert len(tables) == 1

    def test_multiple_entries(self, db):
        for i in range(5):
            db.upsert_crdt_entry(i, f"agent-{i}", "upsert", "{}", "2026-01-01")
        count = db._test_conn.execute("SELECT COUNT(*) FROM crdt_entries").fetchone()[0]
        assert count == 5
