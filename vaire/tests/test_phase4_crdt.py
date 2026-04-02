"""Tests for Phase 4: CRDT agent propagation."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from vaire.config import Settings
from vaire.crdt_sync import CRDTMemorySync
from vaire.socket_server import VaireSocketServer
from vaire.storage import StorageEngine


@pytest.fixture
def settings():
    return Settings(DB_PATH=":memory:", CRDT_AGENT_ID="test-agent")


@pytest.fixture
def storage(tmp_path):
    db_path = str(tmp_path / "crdt_phase4.db")
    engine = StorageEngine(db_path)
    yield engine
    engine.close()


@pytest.fixture
def crdt(storage, settings):
    return CRDTMemorySync(storage, settings)


def _insert_memory(storage: StorageEngine, content: str = "hello") -> int:
    return storage.insert_memory(
        {
            "content": content,
            "embedding": None,
            "tags": [],
            "directory_context": "/tmp",
            "heat": 1.0,
        }
    )


# ── TestSetActiveAgent ─────────────────────────────────────────────────────────

class TestSetActiveAgent:
    def test_rejects_empty_string(self, crdt):
        with pytest.raises(ValueError):
            crdt.set_active_agent("")

    def test_rejects_default(self, crdt):
        with pytest.raises(ValueError):
            crdt.set_active_agent("default")

    def test_accepts_valid_agent_id(self, crdt):
        crdt.set_active_agent("agent-beta")
        assert crdt.get_active_agent() == "agent-beta"


# ── TestGetActiveAgent ─────────────────────────────────────────────────────────

class TestGetActiveAgent:
    def test_returns_what_was_set(self, crdt):
        crdt.set_active_agent("agent-gamma")
        assert crdt.get_active_agent() == "agent-gamma"

    def test_empty_before_set(self, crdt):
        assert crdt.get_active_agent() == ""


# ── TestTagWrite ───────────────────────────────────────────────────────────────

class TestTagWrite:
    def test_requires_set_active_agent_first(self, crdt, storage):
        mid = _insert_memory(storage)
        with pytest.raises(RuntimeError, match="tag_write called before set_active_agent"):
            crdt.tag_write(mid, "upsert")

    def test_stores_crdt_entry_in_db(self, crdt, storage):
        crdt.set_active_agent("writer-agent")
        mid = _insert_memory(storage)
        crdt.tag_write(mid, "upsert")

        rows = storage._test_conn.execute(
            "SELECT * FROM crdt_entries WHERE memory_id = ?", (mid,)
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["agent_id"] == "writer-agent"
        assert rows[0]["operation"] == "upsert"

    def test_vector_clock_increments_per_write(self, crdt, storage):
        crdt.set_active_agent("clock-agent")
        mid = _insert_memory(storage, "c1")
        crdt.tag_write(mid, "upsert")
        crdt.tag_write(mid, "update")

        rows = storage._test_conn.execute(
            "SELECT vector_clock FROM crdt_entries ORDER BY id"
        ).fetchall()
        clock1 = json.loads(rows[0]["vector_clock"])
        clock2 = json.loads(rows[1]["vector_clock"])
        assert clock2["clock-agent"] > clock1["clock-agent"]

    def test_two_agents_produce_distinct_clock_entries(self, crdt, storage):
        mid = _insert_memory(storage, "shared")

        crdt.set_active_agent("agent-A")
        crdt.tag_write(mid, "write")

        crdt.set_active_agent("agent-B")
        crdt.tag_write(mid, "write")

        rows = storage._test_conn.execute("SELECT agent_id FROM crdt_entries").fetchall()
        agents = [r["agent_id"] for r in rows]
        assert "agent-A" in agents
        assert "agent-B" in agents


# ── TestSocketServerCrdtWiring ─────────────────────────────────────────────────

class TestSocketServerCrdtWiring:
    @pytest.mark.anyio
    async def test_set_active_agent_called_on_dispatch(self, tmp_path):
        mock_crdt = MagicMock()

        async def echo_handler(**kwargs):
            return {"ok": True}

        server = VaireSocketServer(
            socket_path=str(tmp_path / "test.sock"),
            pid_file=str(tmp_path / "test.pid"),
            dispatch_table={"echo": echo_handler},
            crdt=mock_crdt,
        )

        from vaire.socket_server import ConnectionState
        state = ConnectionState(agent_id="agent-xyz", role="agent")

        await server._dispatch_message("req-1", "echo", {}, state)

        mock_crdt.set_active_agent.assert_called_once_with("agent-xyz")

    @pytest.mark.anyio
    async def test_no_crdt_wiring_when_crdt_is_none(self, tmp_path):
        """Server without crdt= still dispatches normally."""
        async def ping(**kwargs):
            return {"pong": True}

        server = VaireSocketServer(
            socket_path=str(tmp_path / "test2.sock"),
            pid_file=str(tmp_path / "test2.pid"),
            dispatch_table={"ping": ping},
        )

        from vaire.socket_server import ConnectionState
        state = ConnectionState(agent_id="agent-abc", role="agent")

        response = await server._dispatch_message("req-2", "ping", {}, state)
        assert response["result"]["pong"] is True
