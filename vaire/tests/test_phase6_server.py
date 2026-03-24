"""Tests for Phase 6: dispatch table building and groomer method routing."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from vaire.socket_server import (
    ConnectionState,
    GROOMER_METHODS,
    VaireSocketServer,
)


# ── TestGroomerMethodsSet ──────────────────────────────────────────────────────


class TestGroomerMethodsSet:
    def test_all_groom_prefixed(self):
        """Every entry in GROOMER_METHODS starts with 'groom_'."""
        for name in GROOMER_METHODS:
            assert name.startswith("groom_"), f"Unexpected name: {name}"

    def test_contains_audit_tools(self):
        expected = {
            "groom_audit", "groom_inspect", "groom_duplicates",
            "groom_contradictions", "groom_orphans", "groom_stale", "groom_stats",
        }
        assert expected <= GROOMER_METHODS

    def test_contains_mutation_tools(self):
        expected = {
            "groom_merge", "groom_split", "groom_retag", "groom_reclassify",
            "groom_update_content", "groom_promote", "groom_demote",
            "groom_bulk_delete", "groom_auto",
        }
        assert expected <= GROOMER_METHODS


# ── TestGroomerMethodRouting ───────────────────────────────────────────────────


def _make_server(tmp_path, groomer_methods=None):
    async def ping(**kwargs):
        return {"pong": True}

    return VaireSocketServer(
        socket_path=str(tmp_path / "test.sock"),
        pid_file=str(tmp_path / "test.pid"),
        dispatch_table={"ping": ping},
        groomer_methods=groomer_methods,
    )


class TestGroomerMethodRouting:
    @pytest.mark.anyio
    async def test_non_groomer_blocked_from_groom_method(self, tmp_path):
        """Regular agents receive FORBIDDEN for groom_* methods."""
        server = _make_server(tmp_path)
        state = ConnectionState(agent_id="regular-agent", role="agent")
        response = await server._dispatch_message("req-1", "groom_audit", {}, state)
        assert response["status"] == "error"
        assert response["code"] == "FORBIDDEN"

    @pytest.mark.anyio
    async def test_groomer_can_call_groom_method(self, tmp_path):
        """Groomer agents can call groom_* methods from groomer_methods table."""
        async def groom_audit(**kwargs):
            return {"audited": True}

        server = _make_server(tmp_path, groomer_methods={"groom_audit": groom_audit})
        state = ConnectionState(agent_id="groomer-agent-1", role="groomer")
        response = await server._dispatch_message("req-2", "groom_audit", {}, state)
        assert response["status"] == "ok"
        assert response["result"]["audited"] is True

    @pytest.mark.anyio
    async def test_groomer_can_call_regular_method(self, tmp_path):
        """Groomer agents can still access the base dispatch table methods."""
        server = _make_server(tmp_path)
        state = ConnectionState(agent_id="groomer-agent-1", role="groomer")
        response = await server._dispatch_message("req-3", "ping", {}, state)
        assert response["status"] == "ok"
        assert response["result"]["pong"] is True

    @pytest.mark.anyio
    async def test_groom_method_not_found_without_groomer_table(self, tmp_path):
        """A groomer agent calling groom_* with no groomer_methods gets NOT_FOUND."""
        server = _make_server(tmp_path)  # no groomer_methods
        state = ConnectionState(agent_id="groomer-agent-1", role="groomer")
        response = await server._dispatch_message("req-4", "groom_stats", {}, state)
        assert response["status"] == "error"
        assert response["code"] == "NOT_FOUND"

    @pytest.mark.anyio
    async def test_groomer_methods_overrides_dispatch_table(self, tmp_path):
        """If both tables have a key, groomer_methods takes precedence."""
        async def ping_groomer(**kwargs):
            return {"source": "groomer"}

        server = VaireSocketServer(
            socket_path=str(tmp_path / "ovr.sock"),
            pid_file=str(tmp_path / "ovr.pid"),
            dispatch_table={"ping": lambda **kw: {"source": "table"}},
            groomer_methods={"ping": ping_groomer},
        )
        state = ConnectionState(agent_id="groomer-x", role="groomer")
        response = await server._dispatch_message("req-5", "ping", {}, state)
        assert response["result"]["source"] == "groomer"


# ── TestBuildDispatchTable ─────────────────────────────────────────────────────


class TestBuildDispatchTable:
    def test_returns_core_method_names(self):
        """build_dispatch_table() includes all core MCP tool names."""
        import vaire.server as srv

        # Provide minimal mocks so the builder doesn't crash looking up globals.
        # (The builder just wraps function references — no engine state needed.)
        table = srv.build_dispatch_table()

        expected_keys = {
            "remember", "recall", "forget", "get_project_context",
            "consolidate_now", "memory_stats", "rate_memory", "validate_memory",
            "recall_hierarchical", "drill_down", "create_trigger",
            "get_project_story", "add_rule", "get_rules", "navigate_memory",
            "get_causal_chain", "assess_coverage", "detect_gaps",
            "checkpoint", "restore", "anchor", "install_hooks", "sync_instructions",
        }
        assert expected_keys <= set(table.keys())

    def test_handlers_are_async(self):
        """Every handler in the dispatch table is a coroutine function."""
        import asyncio
        import vaire.server as srv

        table = srv.build_dispatch_table()
        for name, handler in table.items():
            assert asyncio.iscoroutinefunction(handler), f"{name} is not async"

    def test_build_groomer_dispatch_empty_without_engine(self):
        """build_groomer_dispatch() returns {} when _groomer is None."""
        import vaire.server as srv

        original = srv._groomer
        srv._groomer = None
        try:
            result = srv.build_groomer_dispatch()
            assert result == {}
        finally:
            srv._groomer = original

    def test_build_groomer_dispatch_returns_all_methods(self):
        """build_groomer_dispatch() exposes all 16 groomer methods."""
        import vaire.server as srv

        mock_groomer = MagicMock()
        srv._groomer = mock_groomer
        try:
            table = srv.build_groomer_dispatch()
        finally:
            srv._groomer = None

        assert set(table.keys()) == GROOMER_METHODS

    def test_build_ingest_dispatch_returns_four_methods(self):
        """build_ingest_dispatch() returns the four ingestion methods."""
        import vaire.server as srv

        mock_pipeline = MagicMock()
        table = srv.build_ingest_dispatch(mock_pipeline)

        assert set(table.keys()) == {
            "ingest_file", "ingest_directory", "ingest_status", "ingest_preview"
        }

    @pytest.mark.anyio
    async def test_ingest_dispatch_forwards_params_correctly(self):
        """ingest_file wrapper repackages **kwargs into (params, agent_id)."""
        import vaire.server as srv

        mock_pipeline = MagicMock()
        mock_pipeline.ingest_file = AsyncMock(return_value={"status": "done"})

        table = srv.build_ingest_dispatch(mock_pipeline)
        result = await table["ingest_file"](
            file_path="/tmp/doc.md", dry_run=False, agent_id="agent-test"
        )

        mock_pipeline.ingest_file.assert_called_once_with(
            {"file_path": "/tmp/doc.md", "dry_run": False}, "agent-test"
        )
        assert result == {"status": "done"}
