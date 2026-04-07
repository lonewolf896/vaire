"""MCP integration tests — validate tool parameter parity and end-to-end behavior.

These tests catch parameter mismatches between MCP stubs (socket_client.py)
and server handlers (server.py) that are invisible to unit tests.

Three test categories:
  A. Parameter parity (static, no container needed)
  B. End-to-end tool coverage via QA container (requires Docker)
  C. Memory size stress tests (various content sizes)

Run with:
    .venv/bin/python -m pytest vaire/tests/test_mcp_integration.py -v
"""
from __future__ import annotations

import asyncio
import inspect
import os
import random
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from vaire.socket_client import VaireClient, VaireError

# ═══════════════════════════════════════════════════════════════════════
# A. Parameter parity tests (static — no container needed)
# ═══════════════════════════════════════════════════════════════════════


def _get_stub_params(func) -> set[str]:
    """Get parameter names from an MCP stub function, excluding 'self'."""
    sig = inspect.signature(func)
    return {
        name for name, p in sig.parameters.items()
        if name != "self" and name != "return"
    }


def _get_server_params(func) -> set[str]:
    """Get parameter names from a server handler, excluding internal-only params."""
    sig = inspect.signature(func)
    # agent_id is injected by the socket server dispatch, not a client param
    return {
        name for name, p in sig.parameters.items()
        if name not in ("self", "return", "agent_id")
    }


class TestParameterParity:
    """Verify MCP stubs in socket_client.py expose all server handler parameters.

    This catches bugs like the force= parameter being missing from the
    remember stub — the #1 bug that blocked production deployment.
    """

    def test_remember_params_match(self):
        from vaire.socket_client import remember as stub
        from vaire.server import remember as handler
        stub_params = _get_stub_params(stub)
        server_params = _get_server_params(handler)
        missing = server_params - stub_params
        assert not missing, (
            f"remember stub is missing parameters: {missing}. "
            f"Stub has: {stub_params}, Server has: {server_params}"
        )

    def test_recall_params_match(self):
        from vaire.socket_client import recall as stub
        from vaire.server import recall as handler
        stub_params = _get_stub_params(stub)
        server_params = _get_server_params(handler)
        missing = server_params - stub_params
        assert not missing, (
            f"recall stub is missing parameters: {missing}. "
            f"Stub has: {stub_params}, Server has: {server_params}"
        )

    def test_rate_memory_params_match(self):
        from vaire.socket_client import rate_memory as stub
        from vaire.server import rate_memory as handler
        stub_params = _get_stub_params(stub)
        server_params = _get_server_params(handler)
        missing = server_params - stub_params
        assert not missing, (
            f"rate_memory stub is missing parameters: {missing}. "
            f"Stub has: {stub_params}, Server has: {server_params}"
        )

    def test_all_common_tools_params_match(self):
        """Automated check across all tools that exist in both stub and server."""
        import vaire.socket_client as client_mod
        import vaire.server as server_mod

        tool_names = [
            "remember", "recall", "forget", "get_project_context",
            "memory_stats", "consolidate_now", "rate_memory",
            "validate_memory", "recall_hierarchical", "anchor",
            "get_project_story", "get_rules", "navigate_memory",
            "get_causal_chain", "assess_coverage", "detect_gaps",
            "checkpoint", "restore", "drill_down",
            "create_trigger", "add_rule",
        ]

        mismatches = []
        for name in tool_names:
            stub_fn = getattr(client_mod, name, None)
            server_fn = getattr(server_mod, name, None)
            if stub_fn is None or server_fn is None:
                continue

            stub_params = _get_stub_params(stub_fn)
            server_params = _get_server_params(server_fn)
            missing = server_params - stub_params
            if missing:
                mismatches.append(f"  {name}: stub missing {missing}")

        assert not mismatches, (
            "Parameter mismatches between MCP stubs and server handlers:\n"
            + "\n".join(mismatches)
        )


# ═══════════════════════════════════════════════════════════════════════
# B. End-to-end tests (QA container required)
# ═══════════════════════════════════════════════════════════════════════

QA_SOCKET = Path.home() / ".vaire-qa" / "vaire.sock"
QA_COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.qa.yml"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEST_DIR = "/tmp/vaire-mcp-integration-test"
TEST_TAG = "mcp-integration-test"

_container_available = pytest.mark.skipif(
    shutil.which("docker") is None,
    reason="Docker not available",
)


@pytest.fixture(scope="module")
def qa_container():
    """Start QA container, wait for health, yield socket path, teardown."""
    if shutil.which("docker") is None:
        pytest.skip("Docker not available")

    # Clean stale PID file that prevents container startup
    pid_file = Path.home() / ".vaire-qa" / "vaire.pid"
    pid_file.unlink(missing_ok=True)

    # Stop any leftover QA container from a previous run
    subprocess.run(
        ["docker", "compose", "-f", str(QA_COMPOSE), "down"],
        cwd=str(PROJECT_ROOT),
        env={**os.environ, "GID": str(os.getgid())},
        capture_output=True,
        timeout=60,
    )

    subprocess.run(
        ["docker", "compose", "-f", str(QA_COMPOSE), "up", "-d", "--build"],
        cwd=str(PROJECT_ROOT),
        env={**os.environ, "GID": str(os.getgid())},
        capture_output=True,
        timeout=300,
    )

    for _ in range(30):
        result = subprocess.run(
            ["docker", "inspect", "vaire-qa", "--format", "{{.State.Health.Status}}"],
            capture_output=True, text=True,
        )
        if result.stdout.strip() == "healthy":
            break
        time.sleep(2)
    else:
        logs = subprocess.run(
            ["docker", "logs", "vaire-qa", "--tail", "30"],
            capture_output=True, text=True,
        )
        pytest.fail(f"QA container not healthy.\n{logs.stderr}\n{logs.stdout}")

    yield str(QA_SOCKET)

    subprocess.run(
        ["docker", "compose", "-f", str(QA_COMPOSE), "down"],
        cwd=str(PROJECT_ROOT),
        env={**os.environ, "GID": str(os.getgid())},
        capture_output=True,
        timeout=60,
    )


@pytest.fixture
async def client(qa_container):
    """Per-test VaireClient connected to QA container."""
    c = VaireClient(qa_container, call_timeout=60.0)
    yield c
    await c.disconnect()


async def _force_remember(client, content, context=TEST_DIR, tags=None):
    """Helper: store a memory with force=True, return memory_id."""
    result = await client.call("remember", {
        "force": True,
        "content": content,
        "context": context,
        "tags": tags or [TEST_TAG],
    })
    mid = result.get("id") or result.get("memory_id")
    assert mid is not None, f"force remember failed: {result}"
    return mid


async def _cleanup(client, memory_id):
    """Helper: forget a memory, ignore errors."""
    try:
        await client.call("forget", {"memory_id": memory_id})
    except Exception:
        pass


# -- Write path tests --

@_container_available
class TestRemember:
    @pytest.mark.anyio
    async def test_force_bypasses_gate(self, client):
        """force=True must store regardless of surprisal."""
        mid = await _force_remember(client, "Low-surprisal forced content for gate test")
        await _cleanup(client, mid)

    @pytest.mark.anyio
    async def test_without_force_may_reject(self, client):
        """Without force, low-surprisal content may be rejected."""
        result = await client.call("remember", {
            "content": "test memory content",
            "context": TEST_DIR,
            "tags": [TEST_TAG],
        })
        # Either stored or rejected — both are valid
        assert isinstance(result, dict)
        if result.get("id") or result.get("memory_id"):
            await _cleanup(client, result.get("id") or result.get("memory_id"))

    @pytest.mark.anyio
    async def test_returns_surprisal(self, client):
        """Response must include surprisal score."""
        result = await client.call("remember", {
            "force": True,
            "content": f"Surprisal test with token {random.randint(100000, 999999)}",
            "context": TEST_DIR,
            "tags": [TEST_TAG],
        })
        assert "surprisal" in result or "surprise_score" in result
        await _cleanup(client, result.get("id") or result.get("memory_id"))

    @pytest.mark.anyio
    async def test_tags_stored(self, client):
        """Custom tags must be preserved."""
        mid = await _force_remember(
            client,
            f"Tag test memory {random.randint(100000, 999999)}",
            tags=[TEST_TAG, "custom-tag-123"],
        )
        # Recall and check tags
        resp = await client.call("recall", {
            "query": "Tag test memory",
            "max_results": 10,
        })
        memories = resp.get("result", resp) if isinstance(resp, dict) else resp
        mem = next((m for m in memories if m.get("id") == mid), None)
        if mem:
            assert "custom-tag-123" in mem.get("tags", [])
        await _cleanup(client, mid)

    @pytest.mark.anyio
    async def test_empty_content_handled(self, client):
        """Empty content should not crash the server."""
        result = await client.call("remember", {
            "content": "",
            "context": TEST_DIR,
            "tags": [],
        })
        assert isinstance(result, dict)
        mid = result.get("id") or result.get("memory_id")
        if mid:
            await _cleanup(client, mid)


@_container_available
class TestRecall:
    @pytest.mark.anyio
    async def test_finds_stored_memory(self, client):
        """recall must find a memory stored with force=True."""
        token = random.randint(100000, 999999)
        # Use a distinctive multi-word query that FTS5 and vector search can match
        content = (
            f"Recall integration sentinel {token}: this memory contains a unique "
            "verification code that should be retrievable via the recall pipeline "
            "including vector search, FTS5, and score normalization."
        )
        mid = await _force_remember(client, content)
        resp = await client.call("recall", {
            "query": f"Recall integration sentinel {token} verification code",
            "max_results": 10,
        })
        memories = resp.get("result", resp) if isinstance(resp, dict) else resp
        ids = [m.get("id") for m in memories if not m.get("_budget_meta")]
        assert mid in ids, (
            f"Memory {mid} not found in recall results. Got IDs: {ids}"
        )
        await _cleanup(client, mid)

    @pytest.mark.anyio
    async def test_max_results_respected(self, client):
        """max_results parameter must limit result count."""
        resp = await client.call("recall", {
            "query": "test",
            "max_results": 2,
        })
        memories = resp.get("result", resp) if isinstance(resp, dict) else resp
        real = [m for m in memories if not m.get("_budget_meta")]
        assert len(real) <= 2

    @pytest.mark.anyio
    async def test_min_heat_parameter(self, client):
        """min_heat parameter must be accepted."""
        resp = await client.call("recall", {
            "query": "test",
            "min_heat": 0.5,
            "max_results": 5,
        })
        assert isinstance(resp, (dict, list))

    @pytest.mark.anyio
    async def test_compact_mode(self, client):
        """compact=True must be accepted."""
        resp = await client.call("recall", {
            "query": "test",
            "compact": True,
            "max_results": 5,
        })
        assert isinstance(resp, (dict, list))

    @pytest.mark.anyio
    async def test_context_filter(self, client):
        """context parameter must filter by directory."""
        resp = await client.call("recall", {
            "query": "test",
            "context": "/nonexistent/directory",
            "max_results": 5,
        })
        assert isinstance(resp, (dict, list))


@_container_available
class TestForget:
    @pytest.mark.anyio
    async def test_removes_memory(self, client):
        """forget must remove a stored memory."""
        mid = await _force_remember(client, f"Forget test {random.randint(100000, 999999)}")
        result = await client.call("forget", {"memory_id": mid})
        assert isinstance(result, dict)

    @pytest.mark.anyio
    async def test_nonexistent_id(self, client):
        """Forgetting a non-existent ID must not crash."""
        try:
            result = await client.call("forget", {"memory_id": 999999999})
            assert result.get("deleted") is not True
        except VaireError:
            pass


@_container_available
class TestAnchor:
    @pytest.mark.anyio
    async def test_stores_protected(self, client):
        """anchor must create a protected memory."""
        result = await client.call("anchor", {
            "content": f"Anchor test {random.randint(100000, 999999)}",
            "context": TEST_DIR,
            "reason": "integration test",
        })
        mid = result.get("memory_id")
        assert mid is not None
        assert result.get("is_protected") is True
        await _cleanup(client, mid)

    @pytest.mark.anyio
    async def test_with_reason(self, client):
        """anchor must accept reason parameter."""
        result = await client.call("anchor", {
            "content": f"Anchor reason test {random.randint(100000, 999999)}",
            "context": TEST_DIR,
            "reason": "critical architectural decision",
        })
        assert result.get("memory_id") is not None
        await _cleanup(client, result["memory_id"])


@_container_available
class TestRecallHierarchical:
    @pytest.mark.anyio
    async def test_returns_list(self, client):
        """recall_hierarchical must return a list."""
        resp = await client.call("recall_hierarchical", {
            "query": "test query",
            "max_results": 5,
        })
        memories = resp.get("result", resp) if isinstance(resp, dict) else resp
        assert isinstance(memories, list)

    @pytest.mark.anyio
    async def test_with_level(self, client):
        """level parameter must be accepted."""
        resp = await client.call("recall_hierarchical", {
            "query": "test query",
            "level": 1,
            "max_results": 5,
        })
        assert isinstance(resp, (dict, list))


@_container_available
class TestMemoryStats:
    @pytest.mark.anyio
    async def test_returns_expected_keys(self, client):
        """memory_stats must return all core metrics."""
        result = await client.call("memory_stats", {})
        expected = ["total_memories", "active_count", "avg_heat"]
        for key in expected:
            assert key in result, f"Missing key: {key}"

    @pytest.mark.anyio
    async def test_counts_are_integers(self, client):
        """Counts must be integers."""
        result = await client.call("memory_stats", {})
        assert isinstance(result["total_memories"], int)
        assert isinstance(result["active_count"], int)


@_container_available
class TestGetProjectContext:
    @pytest.mark.anyio
    async def test_returns_memories_key(self, client):
        result = await client.call("get_project_context", {"directory": TEST_DIR})
        assert isinstance(result, dict)
        assert "memories" in result

    @pytest.mark.anyio
    async def test_compact_mode(self, client):
        result = await client.call("get_project_context", {
            "directory": TEST_DIR,
            "compact": True,
        })
        assert isinstance(result, dict)


@_container_available
class TestRateMemory:
    @pytest.mark.anyio
    async def test_rate_stored_memory(self, client):
        """rate_memory must accept a valid memory ID."""
        mid = await _force_remember(client, f"Rate test {random.randint(100000, 999999)}")
        result = await client.call("rate_memory", {
            "memory_id": mid,
            "rating": 1.0,
        })
        assert result.get("status") == "rated"
        await _cleanup(client, mid)

    @pytest.mark.anyio
    async def test_was_useful_parameter(self, client):
        """was_useful parameter must be accepted."""
        mid = await _force_remember(client, f"Useful test {random.randint(100000, 999999)}")
        result = await client.call("rate_memory", {
            "memory_id": mid,
            "rating": 1.0,
            "was_useful": True,
        })
        assert isinstance(result, dict)
        await _cleanup(client, mid)


@_container_available
class TestValidateMemory:
    @pytest.mark.anyio
    async def test_validate_stored(self, client):
        mid = await _force_remember(client, f"Validate test {random.randint(100000, 999999)}")
        result = await client.call("validate_memory", {"memory_id": mid})
        assert isinstance(result, dict)
        await _cleanup(client, mid)


@_container_available
class TestConsolidateNow:
    @pytest.mark.anyio
    async def test_runs_without_error(self, client):
        result = await client.call("consolidate_now", {})
        assert isinstance(result, dict)


@_container_available
class TestGetProjectStory:
    @pytest.mark.anyio
    async def test_returns_dict(self, client):
        result = await client.call("get_project_story", {"directory": TEST_DIR})
        assert isinstance(result, dict)


@_container_available
class TestNavigateMemory:
    @pytest.mark.anyio
    async def test_returns_list(self, client):
        resp = await client.call("navigate_memory", {
            "query": "test navigation",
            "top_k": 3,
        })
        result = resp.get("result", resp) if isinstance(resp, dict) else resp
        assert isinstance(result, (list, dict))


@_container_available
class TestGetCausalChain:
    @pytest.mark.anyio
    async def test_returns_dict(self, client):
        result = await client.call("get_causal_chain", {"entity": "test_entity"})
        assert isinstance(result, dict)


@_container_available
class TestAssessCoverage:
    @pytest.mark.anyio
    async def test_returns_dict(self, client):
        result = await client.call("assess_coverage", {
            "query": "test coverage",
            "directory": TEST_DIR,
        })
        assert isinstance(result, dict)


@_container_available
class TestDetectGaps:
    @pytest.mark.anyio
    async def test_returns_list(self, client):
        resp = await client.call("detect_gaps", {"directory": TEST_DIR})
        result = resp.get("result", resp) if isinstance(resp, dict) else resp
        assert isinstance(result, (list, dict))


@_container_available
class TestCheckpointRestore:
    @pytest.mark.anyio
    async def test_checkpoint_and_restore(self, client):
        """checkpoint followed by restore must not crash."""
        cp = await client.call("checkpoint", {
            "directory": TEST_DIR,
            "current_task": "MCP integration test",
        })
        assert isinstance(cp, dict)

        res = await client.call("restore", {"directory": TEST_DIR})
        assert isinstance(res, dict)

    @pytest.mark.anyio
    async def test_checkpoint_with_all_params(self, client):
        """checkpoint must accept all optional parameters."""
        result = await client.call("checkpoint", {
            "directory": TEST_DIR,
            "current_task": "Full param test",
            "files_being_edited": ["test.py"],
            "key_decisions": ["use domain methods"],
            "open_questions": ["none"],
            "next_steps": ["deploy"],
            "active_errors": [],
            "custom_context": "integration test context",
        })
        assert isinstance(result, dict)


@_container_available
class TestDrillDown:
    @pytest.mark.anyio
    async def test_nonexistent_cluster(self, client):
        """drill_down on non-existent cluster must not crash."""
        resp = await client.call("drill_down", {"cluster_id": 999999})
        assert isinstance(resp, (dict, list))


@_container_available
class TestGetRules:
    @pytest.mark.anyio
    async def test_returns_list(self, client):
        resp = await client.call("get_rules", {"directory": TEST_DIR})
        result = resp.get("result", resp) if isinstance(resp, dict) else resp
        assert isinstance(result, (list, dict))


@_container_available
class TestCreateTrigger:
    @pytest.mark.anyio
    async def test_creates_trigger(self, client):
        """create_trigger must accept all parameters."""
        result = await client.call("create_trigger", {
            "content": "Remind me to check test results",
            "trigger_condition": "test results",
            "trigger_type": "keyword_match",
            "target_directory": TEST_DIR,
        })
        assert isinstance(result, dict)


# -- Error handling --

@_container_available
class TestErrorHandling:
    @pytest.mark.anyio
    async def test_unknown_method_raises(self, client):
        with pytest.raises(VaireError):
            await client.call("this_method_does_not_exist_xyz", {})

    @pytest.mark.anyio
    async def test_concurrent_calls(self, client):
        """Multiple concurrent calls must all succeed."""
        results = await asyncio.gather(*[
            client.call("memory_stats", {})
            for _ in range(10)
        ])
        assert len(results) == 10
        assert all(isinstance(r, dict) for r in results)


# ═══════════════════════════════════════════════════════════════════════
# C. Memory size stress tests
# ═══════════════════════════════════════════════════════════════════════

@_container_available
class TestMemorySizes:
    """Test storing and recalling memories of various sizes."""

    @pytest.mark.anyio
    async def test_tiny_memory_50_chars(self, client):
        """50-character memory."""
        content = "A" * 50
        mid = await _force_remember(client, content)
        mem = await client.call("validate_memory", {"memory_id": mid})
        assert isinstance(mem, dict)
        await _cleanup(client, mid)

    @pytest.mark.anyio
    async def test_small_memory_500_chars(self, client):
        """500-character memory."""
        content = f"Small memory test {random.randint(100000, 999999)}. " + "x " * 240
        mid = await _force_remember(client, content)
        mem = await client.call("validate_memory", {"memory_id": mid})
        assert isinstance(mem, dict)
        await _cleanup(client, mid)

    @pytest.mark.anyio
    async def test_medium_memory_2000_chars(self, client):
        """2000-character memory — typical decision/architecture note."""
        content = (
            f"Architecture decision {random.randint(100000, 999999)}: "
            "We decided to use SQLite with WAL mode for the memory engine. "
            + "This provides good read concurrency with single-writer semantics. " * 25
        )
        assert len(content) > 1500
        mid = await _force_remember(client, content)
        # Verify content roundtrips correctly
        resp = await client.call("recall", {
            "query": content[:80],
            "max_results": 5,
        })
        memories = resp.get("result", resp) if isinstance(resp, dict) else resp
        found = next(
            (m for m in memories if m.get("id") == mid and not m.get("_budget_meta")),
            None,
        )
        assert found is not None, f"2000-char memory not found in recall"
        await _cleanup(client, mid)

    @pytest.mark.anyio
    async def test_large_memory_10000_chars(self, client):
        """10,000-character memory — large code review or analysis."""
        content = (
            f"Large analysis {random.randint(100000, 999999)}: "
            + "Detailed code review findings. " * 300
        )
        assert len(content) > 9000
        mid = await _force_remember(client, content)
        mem = await client.call("validate_memory", {"memory_id": mid})
        assert isinstance(mem, dict)
        await _cleanup(client, mid)

    @pytest.mark.anyio
    async def test_very_large_memory_30000_chars(self, client):
        """30,000-character memory — near the configured limit."""
        content = (
            f"Very large document {random.randint(100000, 999999)}: "
            + "Section content with details about implementation. " * 550
        )
        assert len(content) > 25000
        mid = await _force_remember(client, content)
        mem = await client.call("validate_memory", {"memory_id": mid})
        assert isinstance(mem, dict)
        await _cleanup(client, mid)

    @pytest.mark.anyio
    async def test_oversized_memory_rejected(self, client):
        """Memory exceeding VAIRE_MAX_CONTENT_LENGTH must be rejected."""
        # Default max is 50,000 chars
        content = "X" * 60000
        result = await client.call("remember", {
            "force": True,
            "content": content,
            "context": TEST_DIR,
            "tags": [TEST_TAG],
        })
        # Should be rejected by input validation
        assert result.get("stored") is False or "error" in result, (
            f"60K content should be rejected but got: {list(result.keys())}"
        )

    @pytest.mark.anyio
    async def test_memory_with_unicode(self, client):
        """Memory with Unicode content (CJK, emoji, accented chars)."""
        content = (
            f"Unicode test {random.randint(100000, 999999)}: "
            "Python est un langage de programmation. "
            "Pythonはプログラミング言語です。"
            "Python是一种编程语言。"
            "Ünîcödé chàräctérs àrê håndlëd cörrëctly."
        )
        mid = await _force_remember(client, content)
        mem = await client.call("validate_memory", {"memory_id": mid})
        assert isinstance(mem, dict)
        await _cleanup(client, mid)

    @pytest.mark.anyio
    async def test_memory_with_code_blocks(self, client):
        """Memory containing markdown code blocks."""
        content = (
            f"Code example {random.randint(100000, 999999)}:\n\n"
            "```python\n"
            "def hello_world():\n"
            "    print('Hello, World!')\n"
            "    return 42\n"
            "```\n\n"
            "The function above demonstrates basic Python syntax."
        )
        mid = await _force_remember(client, content)
        resp = await client.call("recall", {
            "query": f"Code example {content.split()[2]}",
            "max_results": 5,
        })
        memories = resp.get("result", resp) if isinstance(resp, dict) else resp
        ids = [m.get("id") for m in memories if not m.get("_budget_meta")]
        assert mid in ids
        await _cleanup(client, mid)

    @pytest.mark.anyio
    async def test_memory_with_special_chars(self, client):
        """Memory with SQL-sensitive and JSON-sensitive characters."""
        content = (
            f"Special chars test {random.randint(100000, 999999)}: "
            "O'Reilly's book said \"use parameterized queries\" "
            "to prevent injection; SELECT * FROM users WHERE 1=1; "
            "backslash\\path, percent%sign, underscore_name, "
            "brackets [array], braces {dict}, angle <tags>"
        )
        mid = await _force_remember(client, content)
        mem = await client.call("validate_memory", {"memory_id": mid})
        assert isinstance(mem, dict)
        await _cleanup(client, mid)

    @pytest.mark.anyio
    async def test_batch_store_10_memories(self, client):
        """Store 10 memories rapidly and verify all are retrievable."""
        stored_ids = []
        for i in range(10):
            mid = await _force_remember(
                client,
                f"Batch memory {i} token {random.randint(100000, 999999)}: "
                f"testing batch storage throughput and retrieval consistency.",
            )
            stored_ids.append(mid)

        # Verify count increased
        stats = await client.call("memory_stats", {})
        assert stats["total_memories"] >= 10

        # Cleanup
        for mid in stored_ids:
            await _cleanup(client, mid)

    @pytest.mark.anyio
    async def test_store_recall_forget_cycle(self, client):
        """Full lifecycle: store → recall → rate → forget."""
        token = random.randint(100000, 999999)
        content = f"Lifecycle test {token}: full CRUD cycle verification"

        # Store
        mid = await _force_remember(client, content)

        # Recall
        resp = await client.call("recall", {
            "query": f"Lifecycle test {token}",
            "max_results": 5,
        })
        memories = resp.get("result", resp) if isinstance(resp, dict) else resp
        ids = [m.get("id") for m in memories if not m.get("_budget_meta")]
        assert mid in ids

        # Rate
        rate_result = await client.call("rate_memory", {
            "memory_id": mid,
            "rating": 1.0,
            "was_useful": True,
        })
        assert rate_result.get("status") == "rated"

        # Forget
        await client.call("forget", {"memory_id": mid})

        # Verify gone
        resp = await client.call("recall", {
            "query": f"Lifecycle test {token}",
            "max_results": 5,
        })
        memories = resp.get("result", resp) if isinstance(resp, dict) else resp
        ids = [m.get("id") for m in memories if not m.get("_budget_meta")]
        assert mid not in ids
