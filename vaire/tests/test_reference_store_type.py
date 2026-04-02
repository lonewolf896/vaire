"""Tests for the reference store_type: compression skip, heat decay skip, retrieval demotion."""

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from vaire.compression import MemoryCompressor
from vaire.config import Settings
from vaire.embeddings import EmbeddingEngine
from vaire.retrieval import _is_compliance_query
from vaire.storage import StorageEngine


@pytest.fixture
def storage(tmp_path):
    engine = StorageEngine(str(tmp_path / "test_reference.db"))
    yield engine
    engine.close()


@pytest.fixture
def settings():
    return Settings(
        DB_PATH=":memory:",
        COMPRESSION_GIST_AGE_HOURS=168.0,
        COMPRESSION_TAG_AGE_HOURS=720.0,
    )


@pytest.fixture
def mock_embeddings():
    engine = EmbeddingEngine()
    engine._unavailable = True

    def fake_encode(text):
        rng = np.random.RandomState(len(text) % 1000)
        vec = rng.randn(384).astype(np.float32)
        vec = vec / np.linalg.norm(vec)
        return vec.tobytes()

    engine.encode = fake_encode
    return engine


@pytest.fixture
def compressor(storage, mock_embeddings, settings):
    return MemoryCompressor(storage, mock_embeddings, settings)


def _hours_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _make_memory(storage, content="test memory", hours_old=0, **kwargs):
    defaults = {
        "content": content,
        "directory_context": "/test",
        "heat": 1.0,
        "created_at": _hours_ago(hours_old),
        "last_accessed": _hours_ago(hours_old),
    }
    defaults.update(kwargs)
    mid = storage.insert_memory(defaults)

    extra_fields = {}
    for field in ("importance", "surprise_score", "confidence", "access_count",
                  "store_type", "is_protected", "compression_level", "original_content",
                  "content_fidelity"):
        if field in kwargs:
            extra_fields[field] = kwargs[field]

    if extra_fields:
        set_parts = []
        values = []
        for k, v in extra_fields.items():
            set_parts.append(f"{k} = ?")
            if isinstance(v, bool):
                values.append(int(v))
            else:
                values.append(v)
        values.append(mid)
        storage._test_conn.execute(
            f"UPDATE memories SET {', '.join(set_parts)} WHERE id = ?",
            values,
        )
        storage._test_conn.commit()
    return mid


class TestReferenceCompression:
    """Reference memories should never be compressed."""

    def test_reference_schedule_returns_zero(self, compressor, storage):
        """Reference store_type should always return compression level 0."""
        mid = _make_memory(storage, hours_old=2000, store_type="reference")
        mem = storage.get_memory(mid)
        assert compressor.get_compression_schedule(mem) == 0

    def test_reference_skipped_in_cycle(self, compressor, storage):
        """compression_cycle should skip reference memories."""
        _make_memory(storage, hours_old=2000, store_type="reference", content_fidelity="auto")
        _make_memory(storage, hours_old=2000, store_type="episodic", content_fidelity="auto")
        stats = compressor.compression_cycle()
        # The episodic one should compress, the reference one should be skipped
        assert stats["protected_skipped"] >= 1  # reference counted here


class TestReferenceHeatDecay:
    """Reference memories should not undergo heat decay."""

    def test_reference_excluded_from_decay(self, storage, settings, mock_embeddings):
        """Reference memories should keep their heat after decay cycle."""
        from vaire.consolidation import AstrocyteEngine
        mid = _make_memory(
            storage, hours_old=500, store_type="reference",
            content="NIST AC-1 Access Control Policy",
        )
        # Set heat to 1.0
        storage._test_conn.execute("UPDATE memories SET heat = 1.0 WHERE id = ?", (mid,))
        storage._test_conn.commit()

        engine = AstrocyteEngine(storage, mock_embeddings, settings)
        stats = {"memories_updated": 0, "memories_archived": 0}
        engine._apply_decay(stats)

        mem = storage.get_memory(mid)
        assert mem["heat"] == 1.0, "Reference memory heat should not decay"


class TestComplianceQueryDetection:
    """_is_compliance_query should detect compliance-related queries."""

    @pytest.mark.parametrize("query", [
        "NIST 800-53 AC-1 access control",
        "What does 800-53 say about encryption?",
        "CSF framework categories",
        "compliance requirements for auth",
        "security control baseline",
        "SP 800-171 requirements",
        "FISMA compliance status",
        "FedRAMP authorization",
        "RMF process steps",
    ])
    def test_compliance_queries_detected(self, query):
        assert _is_compliance_query(query)

    @pytest.mark.parametrize("query", [
        "how do I configure Authentik RBAC",
        "access control in our k8s cluster",
        "what changed in the last deploy",
        "Vaire retrieval pipeline architecture",
        "fix the login bug",
    ])
    def test_non_compliance_queries_not_detected(self, query):
        assert not _is_compliance_query(query)


class TestReferenceStoreTypePersistence:
    """Verify store_type='reference' can be set and read."""

    def test_set_and_get_reference(self, storage):
        mid = _make_memory(storage, store_type="reference", content="NIST ref data")
        mem = storage.get_memory(mid)
        assert mem["store_type"] == "reference"

    def test_bulk_filter_by_reference(self, storage):
        _make_memory(storage, store_type="reference", content="ref 1")
        _make_memory(storage, store_type="reference", content="ref 2")
        _make_memory(storage, store_type="episodic", content="ep 1")

        results = storage.get_memories_by_filter(store_type="reference")
        assert len(results) == 2
        assert all(r["store_type"] == "reference" for r in results)
