"""Tests for the Write Queue (Phase 5)."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from vaire.config import Settings
from vaire.write_queue import WriteOp, WriteOpPriority, WriteQueue


@pytest.fixture
def settings():
    return Settings(
        DB_PATH=":memory:",
        WRITE_BATCH_SIZE=10,
        WRITE_BATCH_INTERVAL_MS=50,
    )


@pytest.fixture
def mock_storage():
    s = MagicMock()
    s.upsert_memory.return_value = 42
    return s


@pytest.fixture
def mock_cache():
    return MagicMock()


@pytest.fixture
def queue(mock_storage, mock_cache, settings):
    return WriteQueue(mock_storage, mock_cache, settings)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_op(loop: asyncio.AbstractEventLoop, method: str = "upsert_memory", **kwargs) -> WriteOp:
    future = loop.create_future()
    return WriteOp(
        priority=WriteOpPriority.CRITICAL,
        method=method,
        kwargs=kwargs or {"content": "x"},
        future=future,
        callback=None,
    )


# ── TestEnqueueCritical ────────────────────────────────────────────────────────

class TestEnqueueCritical:
    @pytest.mark.anyio
    async def test_returns_storage_result(self, queue, mock_storage):
        mock_storage.upsert_memory.return_value = 7
        task = asyncio.create_task(
            queue.enqueue_critical("upsert_memory", content="x")
        )
        await asyncio.sleep(0)       # yield so the task puts its op on the queue
        await queue._drain_batch()
        result = await task
        assert result == 7

    @pytest.mark.anyio
    async def test_result_available_after_await(self, queue, mock_storage):
        mock_storage.upsert_memory.return_value = 100
        task = asyncio.create_task(
            queue.enqueue_critical("upsert_memory", content="y")
        )
        await asyncio.sleep(0)
        await queue._drain_batch()
        assert await task == 100


# ── TestEnqueueBestEffort ──────────────────────────────────────────────────────

class TestEnqueueBestEffort:
    @pytest.mark.anyio
    async def test_callback_fires_before_enqueue_returns(self, queue):
        call_order: list[str] = []

        def cb():
            call_order.append("callback")

        queue.enqueue_best_effort("upsert_memory", callback=cb, content="z")
        call_order.append("after_enqueue")

        assert call_order == ["callback", "after_enqueue"]

    @pytest.mark.anyio
    async def test_op_lands_on_queue(self, queue):
        queue.enqueue_best_effort("upsert_memory", content="z")
        assert queue._queue.qsize() == 1


# ── TestBatching ───────────────────────────────────────────────────────────────

class TestBatching:
    @pytest.mark.anyio
    async def test_multiple_criticals_one_transaction(self, queue, mock_storage):
        mock_storage.upsert_memory.return_value = 1
        loop = asyncio.get_running_loop()

        # Build 3 ops directly and execute as one batch.
        ops = [_make_op(loop, content=f"c{i}") for i in range(3)]
        queue._execute_batch(ops)

        # BEGIN executed exactly once for all 3 ops.
        conn_mock = mock_storage.get_write_connection()
        begin_calls = [
            c for c in conn_mock.execute.call_args_list
            if c.args and c.args[0] == "BEGIN"
        ]
        assert len(begin_calls) == 1

        # All futures resolved.
        for op in ops:
            assert op.future.done()
            assert op.future.result() == 1


# ── TestStopDrainsQueue ────────────────────────────────────────────────────────

class TestStopDrainsQueue:
    @pytest.mark.anyio
    async def test_ops_committed_before_stop(self, queue, mock_storage):
        mock_storage.upsert_memory.return_value = 5
        task = asyncio.create_task(
            queue.enqueue_critical("upsert_memory", content="drain_test")
        )
        await asyncio.sleep(0)   # let the task put its op on the queue
        await queue.stop()
        result = await task
        assert result == 5


# ── TestExceptionHandling ──────────────────────────────────────────────────────

class TestExceptionHandling:
    @pytest.mark.anyio
    async def test_critical_future_gets_exception(self, queue, mock_storage):
        mock_storage.upsert_memory.side_effect = RuntimeError("DB error")

        task = asyncio.create_task(
            queue.enqueue_critical("upsert_memory", content="fail")
        )
        await asyncio.sleep(0)
        await queue._drain_batch()

        with pytest.raises(RuntimeError, match="DB error"):
            await task

    @pytest.mark.anyio
    async def test_queue_continues_after_failure(self, queue, mock_storage):
        mock_storage.upsert_memory.side_effect = RuntimeError("fail")

        task1 = asyncio.create_task(
            queue.enqueue_critical("upsert_memory", content="fail")
        )
        await asyncio.sleep(0)
        await queue._drain_batch()

        with pytest.raises(RuntimeError):
            await task1

        # Reset: second op should succeed.
        mock_storage.upsert_memory.side_effect = None
        mock_storage.upsert_memory.return_value = 99

        task2 = asyncio.create_task(
            queue.enqueue_critical("upsert_memory", content="ok")
        )
        await asyncio.sleep(0)
        await queue._drain_batch()

        assert await task2 == 99


# ── TestCallbackFired ──────────────────────────────────────────────────────────

class TestCallbackFired:
    @pytest.mark.anyio
    async def test_callback_invoked_after_successful_commit(self, queue, mock_storage):
        called: list[bool] = []

        def cb():
            called.append(True)

        task = asyncio.create_task(
            queue.enqueue_critical("upsert_memory", callback=cb, content="cb_test")
        )
        await asyncio.sleep(0)
        await queue._drain_batch()
        await task

        assert called == [True]
