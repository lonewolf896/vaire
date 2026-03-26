"""Write queue: serialises all SQLite mutations through a single asyncio queue.

CRITICAL operations block the caller until committed.
BEST_EFFORT operations are fire-and-forget; their callback fires synchronously
before the DB write so the cache stays consistent on the hot path.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Callable

from vaire.config import Settings
from vaire.storage import StorageEngine

logger = logging.getLogger(__name__)


class WriteOpPriority(Enum):
    CRITICAL = auto()     # awaitable; caller blocks until committed
    BEST_EFFORT = auto()  # fire-and-forget; no future


@dataclass
class WriteOp:
    priority: WriteOpPriority
    method: str                       # name of StorageEngine method to call
    kwargs: dict
    future: asyncio.Future | None     # None for BEST_EFFORT
    callback: Callable | None         # invoked after commit (cache invalidation)


class WriteQueue:
    """Serialises all StorageEngine writes through a single asyncio queue."""

    def __init__(
        self,
        storage: StorageEngine,
        cache: Any,       # MemoryCache — avoid circular import
        settings: Settings,
    ) -> None:
        self._storage = storage
        self._cache = cache
        self._settings = settings
        self._queue: asyncio.Queue[WriteOp] = asyncio.Queue()
        self._running = False
        self._task: asyncio.Task | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Mark the queue as running.

        The drain task is created lazily on the first enqueue call so that
        start() is safe to call before an asyncio event loop is running
        (e.g. during engine initialisation in the MCP stdio path).
        """
        self._running = True

    def _maybe_start_task(self) -> None:
        """Create the drain task if not yet running. No-op outside an event loop."""
        if self._task is None and self._running:
            try:
                loop = asyncio.get_running_loop()
                self._task = loop.create_task(self._drain_loop())
            except RuntimeError:
                pass  # no running event loop yet — task created on next async call

    async def stop(self) -> None:
        """Cancel the drain loop then do a final drain of any remaining ops."""
        self._running = False
        # Cancel the loop task first so it cannot race against our final drain.
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        # Final drain: picks up ops that arrived since the last drain cycle.
        try:
            await self._drain_all()
        except Exception:
            logger.exception("Error draining write queue on stop; continuing shutdown")

    def _drain_sync(self) -> None:
        """Best-effort synchronous drain for use in signal handlers.

        Dequeues all pending ops and executes them in one transaction without
        an asyncio event loop.  CRITICAL futures are resolved or given an
        exception; BEST_EFFORT ops are committed silently.
        """
        batch: list[WriteOp] = []
        while True:
            try:
                op = self._queue.get_nowait()
                batch.append(op)
            except asyncio.QueueEmpty:
                break
        if batch:
            try:
                self._execute_batch(batch)
            except Exception:
                logger.exception("Error in sync drain during shutdown")

    # ── Public enqueue API ─────────────────────────────────────────────────────

    async def enqueue_critical(
        self,
        method: str,
        callback: Callable | None = None,
        **kwargs: Any,
    ) -> Any:
        """Enqueue a write and await its result.

        Blocks the caller until the op is committed and the result returned.
        """
        self._maybe_start_task()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        op = WriteOp(
            priority=WriteOpPriority.CRITICAL,
            method=method,
            kwargs=kwargs,
            future=future,
            callback=callback,
        )
        await self._queue.put(op)
        return await future

    def enqueue_best_effort(
        self,
        method: str,
        callback: Callable | None = None,
        **kwargs: Any,
    ) -> None:
        """Enqueue a write without waiting for the result.

        If a callback is provided it fires immediately (synchronously) so the
        cache is updated before this call returns; the DB write follows later.
        """
        if callback is not None:
            callback()
        op = WriteOp(
            priority=WriteOpPriority.BEST_EFFORT,
            method=method,
            kwargs=kwargs,
            future=None,
            callback=None,  # already fired above
        )
        self._maybe_start_task()
        self._queue.put_nowait(op)

    # ── Drain loop ─────────────────────────────────────────────────────────────

    async def _drain_loop(self) -> None:
        interval = self._settings.WRITE_BATCH_INTERVAL_MS / 1000.0
        while self._running:
            await asyncio.sleep(interval)
            try:
                await self._drain_batch()
            except Exception:
                logger.exception("Unexpected error in write queue drain batch; loop continues")

    async def _drain_batch(self) -> None:
        batch: list[WriteOp] = []
        limit = self._settings.WRITE_BATCH_SIZE
        while len(batch) < limit:
            try:
                op = self._queue.get_nowait()
                batch.append(op)
            except asyncio.QueueEmpty:
                break
        if batch:
            self._execute_batch(batch)

    # ── Batch execution ────────────────────────────────────────────────────────

    def _execute_batch(self, batch: list[WriteOp]) -> None:
        """Execute all ops in a single transaction.

        Results are collected locally; futures are resolved and callbacks fired
        only after a successful commit but BEFORE the write lock is released.
        This ensures cache invalidation callbacks see consistent state and no
        stale reads can slip in between commit and invalidation.

        On any failure, all CRITICAL futures receive the exception and the
        transaction is rolled back.
        """
        results: list[tuple[WriteOp, Any]] = []
        with self._storage._write_lock:
            try:
                self._storage._in_batch = True
                self._storage._conn.execute("BEGIN")
                for op in batch:
                    result = getattr(self._storage, op.method)(**op.kwargs)
                    results.append((op, result))
                self._storage._conn.commit()
            except Exception as exc:
                try:
                    self._storage._conn.rollback()
                except Exception:
                    pass
                self._storage._in_batch = False

                # Resolve all CRITICAL futures with the exception.
                for op in batch:
                    if op.priority is WriteOpPriority.CRITICAL and op.future is not None:
                        if not op.future.done():
                            op.future.set_exception(exc)

                logger.error("Write batch failed (%d ops): %s", len(batch), exc)
                return
            finally:
                self._storage._in_batch = False

            # Commit succeeded — resolve futures and fire callbacks INSIDE the
            # write lock so cache invalidation completes before any other writer
            # can modify the DB state.
            for op, result in results:
                if op.priority is WriteOpPriority.CRITICAL and op.future is not None:
                    if not op.future.done():
                        op.future.set_result(result)
                if op.callback is not None:
                    try:
                        op.callback()
                    except Exception:
                        logger.exception("Callback raised after commit")

    async def _drain_all(self) -> None:
        """Drain every item currently on the queue in one batch."""
        batch: list[WriteOp] = []
        while True:
            try:
                op = self._queue.get_nowait()
                batch.append(op)
            except asyncio.QueueEmpty:
                break
        if batch:
            self._execute_batch(batch)
